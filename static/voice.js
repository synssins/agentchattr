// Voice: read agent messages aloud (play button, auto-speak per channel) and talk to agents
// hands-free (voice activity detection) or push-to-talk. Speech-to-text runs on the server
// (Whisper over Wyoming); text-to-speech uses each agent's voice from its configuration page.
// The microphone needs a secure page (https or localhost).
const Voice = (() => {
    const ICON = {
        speak: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>',
        talk: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h2M7 8v8M11 5v14M15 8v8M19 11v2"/></svg>',
    };
    const STOP_WORDS = /^(stop|wait|cancel|never ?mind|hold on|shut up)[.!]*$/i;

    const token = () => window.SESSION_TOKEN || window.__SESSION_TOKEN__;
    const post = (path, body, type = 'application/json') => fetch(path, {
        method: 'POST', headers: { 'Content-Type': type, 'X-Session-Token': token() },
        body: type === 'application/json' ? JSON.stringify(body) : body,
    });
    const store = {
        get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
        set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
    };
    const isAgent = (name) => typeof agentConfig !== 'undefined' && !!agentConfig[(name || '').toLowerCase()]
        || (typeof resolveAgent === 'function' && !!resolveAgent((name || '').toLowerCase()));

    // ---------------- Playback ----------------
    // Messages are split into sentences; the next sentence is fetched while one plays,
    // so slow voices (Chatterbox on CPU) start after the first sentence, not the whole text.
    let queue = [];        // [{agent, text, id}]
    let audio = null;      // the playing <audio>
    let playing = null;    // message id being read
    let gen = 0;           // bumps on stop: pending fetches are thrown away

    function sentences(text) {
        const t = (text || '').replace(/```[\s\S]*?```/g, ' (code block) ').replace(/\s+/g, ' ').trim();
        const parts = t.match(/[^.!?]+[.!?]+["')\]]*\s*|[^.!?]+$/g) || [];
        const out = [];
        for (const p of parts) {
            if (out.length && (out[out.length - 1].length < 60 || p.length < 20) && out[out.length - 1].length + p.length < 280) out[out.length - 1] += p;
            else out.push(p);
        }
        return out.map(s => s.trim()).filter(Boolean);
    }

    async function fetchAudio(agent, text) {
        const r = await post('/api/voice/speak', { agent, text });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `speech failed (${r.status})`);
        return URL.createObjectURL(await r.blob());
    }

    function stop() {
        gen++;
        queue = [];
        if (audio) { audio.pause(); audio.src = ''; audio = null; }
        markPlaying(null);
    }

    function markPlaying(id) {
        document.querySelectorAll('.voice-speak-btn.playing').forEach(b => b.classList.remove('playing'));
        playing = id;
        if (id != null) document.querySelectorAll(`.voice-speak-btn[data-id="${id}"]`).forEach(b => b.classList.add('playing'));
        setState();
    }

    function enqueue(msg) {
        queue.push({ agent: msg.sender, text: msg.text || '', id: msg.id });
        if (playing == null) next();
    }

    async function next() {
        const item = queue.shift();
        if (!item) { markPlaying(null); return; }
        const mine = gen;
        markPlaying(item.id);
        const parts = sentences(item.text);
        let pending = parts.length ? fetchAudio(item.agent, parts[0]) : null;
        for (let i = 0; i < parts.length && mine === gen; i++) {
            let url;
            try { url = await pending; } catch (err) { toast(err.message); break; }
            pending = i + 1 < parts.length ? fetchAudio(item.agent, parts[i + 1]) : null;
            if (mine !== gen) { URL.revokeObjectURL(url); break; }
            await new Promise((resolve) => {
                audio = new Audio(url);
                audio.onended = audio.onerror = () => { URL.revokeObjectURL(url); resolve(); };
                audio.play().catch(() => { toast('The browser blocked audio: click anywhere on the page once, then try again.'); resolve(); });
            });
        }
        pending?.then(URL.revokeObjectURL, () => {});
        if (mine === gen) next();
    }

    function toast(text) {
        if (typeof showToast === 'function') showToast(text, 'error'); else console.warn(text);
    }

    // Play button in each message's toolbar (control.js calls this)
    function button(msg) {
        if (!msg || !msg.text || !isAgent(msg.sender)) return null;
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'ctl-tool voice-speak-btn';
        b.dataset.id = msg.id;
        b.title = b.ariaLabel = 'Read aloud';
        b.innerHTML = ICON.speak;
        b.onclick = (e) => {
            e.stopPropagation();
            if (playing === msg.id) { stop(); return; }
            stop();
            enqueue(msg);
        };
        return b;
    }

    // ---------------- Auto-speak (per channel, this browser) ----------------
    const autoKey = () => `agenthub-autospeak-${typeof activeChannel !== 'undefined' ? activeChannel : 'general'}`;
    const autoOn = () => store.get(autoKey(), false);

    Hub.on('message', (ev) => {
        const m = ev.data;
        if (!m || m.type && m.type !== 'chat' || !m.text || m.thread != null) return;
        if ((m.channel || 'general') !== activeChannel || !isAgent(m.sender)) return;
        if (talking && target && m.sender.toLowerCase() === target) waiting = false;
        if (autoOn() || (talking && (!target || m.sender.toLowerCase() === target))) enqueue(m);
    });

    // ---------------- Talking: hands-free and push-to-talk ----------------
    let talking = false;     // hands-free conversation on
    let ptt = false;         // push-to-talk held
    let target = '';         // agent the conversation is with
    let waiting = false;     // sent an utterance, reply not in yet
    let mic = null;          // {ctx, stream, node, rate}
    let frames = [], preroll = [], speaking = false, voiced = 0, silent = 0, floor = 0.01;
    let busy = false;        // transcribing

    async function openMic() {
        if (mic) return mic;
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
            throw new Error('The microphone needs the secure address (https://…:8788).');
        }
        const stream = await navigator.mediaDevices.getUserMedia({ audio: {
            echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const src = ctx.createMediaStreamSource(stream);
        const node = ctx.createScriptProcessor(2048, 1, 1);
        node.onaudioprocess = (e) => onFrame(new Float32Array(e.inputBuffer.getChannelData(0)), ctx.sampleRate);
        src.connect(node);
        node.connect(ctx.destination);  // ScriptProcessor only runs when connected; it outputs silence
        mic = { ctx, stream, node, rate: ctx.sampleRate };
        return mic;
    }

    function closeMic() {
        if (!mic) return;
        mic.node.disconnect();
        mic.stream.getTracks().forEach(t => t.stop());
        mic.ctx.close();
        mic = null;
    }

    // Voice activity detection on signal level, with an adaptive noise floor.
    // ~43 ms frames at 48 kHz: speech starts after 4 loud frames, ends after ~1 s of quiet.
    function onFrame(f, rate) {
        if (ptt) { frames.push(f); return; }
        if (!talking || busy) return;
        let sum = 0;
        for (let i = 0; i < f.length; i++) sum += f[i] * f[i];
        const rms = Math.sqrt(sum / f.length);
        const gate = Math.max(0.012, floor * 3) * (playing != null ? 2 : 1);  // stricter while a voice plays (echo)
        const loud = rms > gate;
        if (!speaking) {
            floor = 0.95 * floor + 0.05 * Math.min(rms, 0.05);
            preroll.push(f);
            if (preroll.length > 8) preroll.shift();
            voiced = loud ? voiced + 1 : 0;
            if (voiced >= 4) {
                speaking = true;
                silent = 0;
                frames = preroll.slice();
                preroll = [];
                if (playing != null) stop();  // barge-in: the owner talks over the agent
                setState();
            }
            return;
        }
        frames.push(f);
        silent = loud ? 0 : silent + 1;
        const secs = frames.length * f.length / rate;
        if (silent * f.length / rate > 1.0 || secs > 30) {
            speaking = false;
            const take = frames;
            frames = [];
            if (secs - silent * f.length / rate > 0.35) finish(take, rate);
            setState();
        }
    }

    function wav(chunks, rate) {
        // Downsample to 16 kHz mono 16-bit: what Whisper wants, and a quarter of the upload
        const total = chunks.reduce((n, c) => n + c.length, 0);
        const ratio = rate / 16000, n = Math.floor(total / ratio);
        const buf = new ArrayBuffer(44 + n * 2), dv = new DataView(buf);
        const all = new Float32Array(total);
        let o = 0;
        for (const c of chunks) { all.set(c, o); o += c.length; }
        const w = (p, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(p + i, s.charCodeAt(i)); };
        w(0, 'RIFF'); dv.setUint32(4, 36 + n * 2, true); w(8, 'WAVE'); w(12, 'fmt ');
        dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
        dv.setUint32(24, 16000, true); dv.setUint32(28, 32000, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
        w(36, 'data'); dv.setUint32(40, n * 2, true);
        for (let i = 0; i < n; i++) {
            const s = Math.max(-1, Math.min(1, all[Math.floor(i * ratio)]));
            dv.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
        }
        return new Blob([buf], { type: 'audio/wav' });
    }

    async function finish(chunks, rate) {
        busy = true;
        setState();
        try {
            const r = await post('/api/voice/transcribe', wav(chunks, rate), 'audio/wav');
            const data = await r.json();
            if (!r.ok) throw new Error(data.error || 'transcription failed');
            const text = (data.text || '').trim();
            if (!text || /^[\[(].*[\])]$/.test(text)) return;  // [BLANK_AUDIO], (noise)
            if (target && STOP_WORDS.test(text)) {
                stop();
                await post('/api/agents/stop', { name: target, channel: activeChannel });
                return;
            }
            const out = target && !/(^|\s)@[\w-]/.test(text) ? `@${target} ${text}` : text;
            if (typeof ws !== 'undefined' && ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'message', text: out, sender: username, channel: activeChannel, attachments: [] }));
                waiting = !!target;
            }
        } catch (err) {
            toast(err.message);
        } finally {
            busy = false;
            setState();
        }
    }

    async function startTalking() {
        try { await openMic(); } catch (err) { toast(err.message); return; }
        talking = true;
        speaking = false; voiced = 0; preroll = [];
        setState();
    }

    function stopTalking() {
        talking = false;
        speaking = false;
        if (!ptt) closeMic();
        stop();
        setState();
    }

    async function pttDown() {
        try { await openMic(); } catch (err) { toast(err.message); return; }
        stop();
        frames = [];
        ptt = true;
        setState();
    }

    function pttUp() {
        if (!ptt) return;
        ptt = false;
        const take = frames;
        frames = [];
        if (mic && take.length) finish(take, mic.rate);
        if (!talking) closeMic();
        setState();
    }

    // ---------------- Controls (next to the mic) ----------------
    let ui = null;

    function agentsHere() {
        if (typeof agentConfig === 'undefined') return [];
        return Object.keys(agentConfig).filter(n => typeof agentInActiveChannel !== 'function' || agentInActiveChannel(n)).sort();
    }

    function setState() {
        if (!ui) return;
        const state = ptt ? 'recording' : busy ? 'thinking' : speaking ? 'hearing' : playing != null ? 'speaking'
            : talking ? (waiting ? 'waiting' : 'listening') : 'off';
        ui.talk.dataset.state = state;
        ui.talk.setAttribute('aria-pressed', String(talking));
        ui.status.textContent = { off: '', listening: 'Listening…', hearing: 'Hearing you…', thinking: 'Transcribing…',
            waiting: `Waiting for ${target || 'a reply'}…`, speaking: 'Speaking… (talk to interrupt)', recording: 'Recording… release to send' }[state];
        ui.bar.hidden = !talking && !ptt && playing == null;
        ui.auto.setAttribute('aria-pressed', String(autoOn()));
        ui.auto.title = autoOn() ? 'Auto-speak is on in this channel' : 'Read new agent replies aloud in this channel';
    }

    function fillTargets() {
        const names = agentsHere();
        const keep = target || store.get('agenthub-voice-target', '');
        ui.target.innerHTML = '<option value="">Anyone (use @name)</option>'
            + names.map(n => `<option value="${escapeHtml(n)}">@${escapeHtml(n)}</option>`).join('');
        ui.target.value = names.includes(keep) ? keep : '';
        target = ui.target.value;
    }

    function mount() {
        const micBtn = document.getElementById('mic');
        if (!micBtn || ui) return;
        const talk = document.createElement('button');
        talk.type = 'button';
        talk.id = 'voice-talk';
        talk.className = 'voice-talk';
        talk.innerHTML = ICON.talk;
        talk.title = 'Talk: click for a hands-free conversation, hold to push-to-talk';
        talk.setAttribute('aria-label', 'Talk to agents');
        const auto = document.createElement('button');
        auto.type = 'button';
        auto.className = 'voice-auto';
        auto.innerHTML = ICON.speak;
        auto.setAttribute('aria-label', 'Auto-speak in this channel');
        micBtn.after(auto, talk);

        const bar = document.createElement('div');
        bar.className = 'voice-bar';
        bar.hidden = true;
        bar.innerHTML = '<span class="voice-dot" aria-hidden="true"></span><span class="voice-status" role="status" aria-live="polite"></span>'
            + '<label class="voice-target-label">Talking to <select class="voice-target" aria-label="Agent to talk to"></select></label>'
            + '<button type="button" class="voice-hush" title="Stop reading aloud">Stop voice</button>'
            + '<button type="button" class="voice-end" title="End the conversation">End</button>';
        const composer = document.getElementById('input-row') || micBtn.parentElement;
        composer.parentElement.insertBefore(bar, composer);
        ui = { talk, auto, bar, status: bar.querySelector('.voice-status'), target: bar.querySelector('.voice-target') };
        ui.target.onchange = () => { target = ui.target.value; store.set('agenthub-voice-target', target); };
        bar.querySelector('.voice-hush').onclick = stop;
        bar.querySelector('.voice-end').onclick = stopTalking;
        auto.onclick = () => { store.set(autoKey(), !autoOn()); if (!autoOn()) stop(); setState(); };

        // Click toggles hands-free; holding (> 350 ms) is push-to-talk
        let held = null, longPress = false;
        talk.addEventListener('pointerdown', (e) => {
            e.preventDefault();
            longPress = false;
            held = setTimeout(() => { longPress = true; pttDown(); }, 350);
        });
        const release = () => {
            clearTimeout(held);
            if (longPress) { pttUp(); return; }
        };
        talk.addEventListener('pointerup', release);
        talk.addEventListener('pointerleave', () => { if (longPress) pttUp(); clearTimeout(held); });
        talk.addEventListener('click', () => {
            if (longPress) { longPress = false; return; }
            if (talking) stopTalking(); else { fillTargets(); startTalking(); }
        });
        Hub.on('status', () => { if (ui && !talking) fillTargets(); });
        fillTargets();
        setState();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
    window.addEventListener('agenthub-channel', () => { stop(); if (ui) { fillTargets(); setState(); } });

    return { button, stop, speak: enqueue, sentences };
})();
window.Voice = Voice;
