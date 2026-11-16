// Settings window (gear icon): categories on the left, settings on the right.
// General reuses the original settings bar (moved in, handlers intact); the other
// sections talk to admin_api.py / agent_runner.py. Secrets are shown once.

(function () {
    const { el, api } = window.SettingsUI;

    const SECTIONS = [
        { group: 'You' },
        { id: 'appearance', label: 'Appearance', render: (p) => renderBar(p, 'appearance', 'Appearance',
            'Theme, type and layout for this room. Changes save automatically.') },
        { id: 'notifications', label: 'Notifications', render: (p) => renderBar(p, 'notifications', 'Notifications',
            'Sounds for new messages, stored in this browser. Each agent can have its own sound on its page under Agents.') },
        { group: 'Room' },
        { id: 'chat', label: 'Chat', render: (p) => renderBar(p, 'chat', 'Chat',
            'Your name, the server label, and how agents take turns. Loop guard: agent-to-agent hops before the room pauses for you (0 = off). Untagged messages: who answers when you name nobody with @; a model can pick the best-suited agent (Picking model: the API agent whose model chooses).') },
        { id: 'llms', label: 'LLMs', render: renderLlms },
        { id: 'agents', label: 'Agents', render: (p) => window.AgentsSection.mount(p) },
        { id: 'rulebooks', label: 'Rulebooks', render: renderRulebooks },
        { group: 'Integrations' },
        { id: 'connections', label: 'Agent access', render: renderConnections },
        { id: 'mcp', label: 'MCP servers', render: renderMcp },
        { id: 'apikeys', label: 'API keys', render: renderApiKeys },
        { id: 'secrets', label: 'Secrets', render: renderSecrets },
        { id: 'voice', label: 'Voice', render: renderVoice },
        { id: 'irc', label: 'IRC', render: renderIrc },
        { group: 'Data' },
        { id: 'metrics', label: 'Usage', render: renderMetrics },
        { id: 'history', label: 'History', render: (p) => renderBar(p, 'history', 'History',
            'Export this room (messages, jobs, rules, summaries) as a zip, or import one.') },
    ];
    let current = 'appearance';

    function dialog() {
        let d = document.getElementById('settings-dialog');
        if (d) return d;
        d = el('dialog', { id: 'settings-dialog', class: 'settings-dialog', 'aria-label': 'Settings' });
        const nav = el('nav', { class: 'settings-nav', 'aria-label': 'Settings categories' },
            el('div', { class: 'settings-nav-title', text: 'Settings' }));
        for (const s of SECTIONS) {
            if (s.group) { nav.append(el('div', { class: 'settings-nav-group', text: s.group })); continue; }
            nav.append(el('button', { class: 'settings-nav-item', 'data-section': s.id, text: s.label,
                onclick: () => show(s.id) }));
        }
        const close = el('button', { class: 'settings-close', 'aria-label': 'Close settings', text: '×',
            onclick: () => d.close() });
        d.append(nav, el('section', { class: 'settings-panel', id: 'settings-panel' }), close);
        d.addEventListener('click', (e) => { if (e.target === d) d.close(); });
        document.body.append(d);
        return d;
    }

    function show(id) {
        current = id;
        const d = dialog();
        d.querySelectorAll('.settings-nav-item').forEach(b =>
            b.setAttribute('aria-current', b.dataset.section === id ? 'page' : 'false'));
        const panel = d.querySelector('#settings-panel');
        // Keep the General controls alive (they hold the original event handlers)
        const bar = document.getElementById('settings-bar');
        if (bar && bar.parentElement === panel) document.body.append(bar);
        if (bar) bar.classList.add('hidden');
        panel.textContent = '';
        (SECTIONS.find(s => s.id === id) || SECTIONS[1]).render(panel);
    }

    function open(section) {
        const d = dialog();
        if (!d.open) d.showModal();
        show(typeof section === 'string' ? section : current);
    }

    function heading(panel, title, intro) {
        panel.append(el('h2', { text: title }));
        if (intro) panel.append(el('p', { class: 'settings-intro', text: intro }));
    }

    function secretBox(label, value, note) {
        const input = el('input', { readonly: true, value, class: 'settings-secret', 'aria-label': label });
        const copy = el('button', { class: 'agents-btn', text: 'Copy', onclick: async () => {
            try { await navigator.clipboard.writeText(value); copy.textContent = 'Copied'; }
            catch { input.select(); }
        } });
        return el('div', { class: 'settings-secret-box' },
            el('strong', { text: label }), el('div', { class: 'agents-inline' }, input, copy),
            el('small', { text: note || 'Shown once. Copy it now; it cannot be shown again.' }));
    }

    function table(headers, rows) {
        const t = el('table', { class: 'settings-table' });
        const tr = el('tr');
        headers.forEach(h => tr.append(el('th', { text: h })));
        t.append(el('thead', {}, tr));
        const body = el('tbody');
        rows.forEach(r => {
            const row = el('tr');
            r.forEach(c => row.append(el('td', {}, typeof c === 'string' ? document.createTextNode(c) : c)));
            body.append(row);
        });
        t.append(body);
        return t;
    }

    // ------------------------------------------------------------ Settings-bar groups
    // The original settings bar keeps its event handlers; each section shows one group of its fields.
    function renderBar(panel, group, title, intro) {
        heading(panel, title, intro);
        const bar = document.getElementById('settings-bar');
        if (!bar) return;
        bar.querySelectorAll('[data-group]').forEach(f => { f.hidden = f.dataset.group !== group; });
        bar.classList.remove('hidden');
        panel.append(bar);
    }

    // ------------------------------------------------------------ Connections
    async function renderConnections(panel) {
        heading(panel, 'Connections',
            'Agents anywhere on your network can join by themselves: tell one to read the URL below and follow it. It registers and receives its own key; you paste nothing.');
        let st;
        try { st = await api('GET', '/api/admin/registration'); }
        catch (e) { panel.append(el('p', { text: e.message })); return; }

        panel.append(secretBox('Join instructions for agents', st.llms_txt,
            'Say to any agent: "Read ' + st.llms_txt + ' and join the room as <name>."'));

        const openReg = el('input', { type: 'checkbox', checked: st.open_registration });
        openReg.addEventListener('change', async () => {
            await api('POST', '/api/admin/registration', { open_registration: openReg.checked });
            renderConnectionsAgain();
        });
        panel.append(el('label', { class: 'agents-toggle settings-row' }, openReg,
            el('span', { text: 'Open registration: any agent on the allowed networks can join without an invite' })));

        panel.append(el('h3', { text: 'Joined agents' }));
        if (!st.agents.length) panel.append(el('p', { class: 'settings-intro', text: 'No agent has joined by itself yet.' }));
        else panel.append(table(['Agent', 'Status', 'Joined', ''], st.agents.map(a => [
            `@${a.name}${a.description ? ' · ' + a.description : ''}`,
            a.online ? 'online' : 'offline', a.joined || '',
            el('button', { class: 'agents-btn danger', text: 'Revoke', onclick: async () => {
                if (!confirm(`Remove ${a.name} and invalidate its key?`)) return;
                await api('DELETE', `/api/admin/remote-agents/${encodeURIComponent(a.name)}`);
                renderConnectionsAgain();
            } })])));

        panel.append(el('h3', { text: 'Invites' }),
            el('p', { class: 'settings-intro', text: 'One-time codes for when open registration is off. Each works once, then dies.' }));
        const label = el('input', { placeholder: 'Label, e.g. "Hermes on laptop"' });
        const ttl = el('input', { type: 'number', min: '1', value: '24', 'aria-label': 'Hours valid' });
        const out = el('div');
        panel.append(el('div', { class: 'agents-inline' }, label, ttl, el('span', { text: 'hours' }),
            el('button', { class: 'agents-btn primary', text: 'Create invite', onclick: async () => {
                const r = await api('POST', '/api/admin/invites', { label: label.value, ttl_hours: Number(ttl.value) });
                out.textContent = '';
                out.append(secretBox('Invite code', r.code));
            } })), out);
        if (st.invites.length) panel.append(table(['Label', 'Status', 'Created', 'Used by', ''], st.invites.map(i => [
            i.label || '(no label)', i.status, i.created, i.used_by || '',
            el('button', { class: 'agents-btn danger', text: 'Delete', onclick: async () => {
                await api('DELETE', `/api/admin/invites/${i.id}`);
                renderConnectionsAgain();
            } })])));
    }
    function renderConnectionsAgain() { if (current === 'connections') show('connections'); }

    // ------------------------------------------------------------ API keys
    async function renderApiKeys(panel) {
        heading(panel, 'API keys',
            'Keys for OpenAI-compatible clients (OpenCode, Hermes, ...). Base URL: ' + location.origin + '/v1. Models: agenthub-room (everyone) or an agent name (that agent only).');
        const name = el('input', { placeholder: 'What connects, e.g. opencode' });
        const out = el('div');
        panel.append(el('div', { class: 'agents-inline' }, name,
            el('button', { class: 'agents-btn primary', text: 'Create key', onclick: async () => {
                try {
                    const r = await api('POST', '/api/admin/v1-keys', { name: name.value });
                    show('apikeys');
                    document.getElementById('settings-panel').insertBefore(
                        secretBox(`Key "${r.name}"`, r.key), document.querySelector('#settings-panel .agents-inline'));
                } catch (e) { alert(e.message); }
            } })), out);
        const keys = await api('GET', '/api/admin/v1-keys');
        if (!keys.length) { panel.append(el('p', { class: 'settings-intro', text: 'No keys yet.' })); return; }
        panel.append(table(['Name', 'Created', 'Last used', ''], keys.map(k => [
            k.name, k.created, k.last_used || 'never',
            el('button', { class: 'agents-btn danger', text: 'Revoke', onclick: async () => {
                if (!confirm(`Revoke key "${k.name}"? Clients using it stop working.`)) return;
                await api('DELETE', `/api/admin/v1-keys/${encodeURIComponent(k.name)}`);
                show('apikeys');
            } })])));
    }

    // ------------------------------------------------------------ Secrets
    async function renderSecrets(panel, editing) {
        heading(panel, 'Secrets',
            'Tokens for outside services (GitHub, cloud CLIs, ...). Each is given only to the agents you tick, as an environment variable in their process. Values are write-only. Agents use them in the commands they run.');
        const [secrets, agents] = await Promise.all([api('GET', '/api/admin/secrets'), api('GET', '/api/managed-agents')]);
        const cli = agents;  // every agent can use secrets in its commands
        const e = editing || {};
        const name = el('input', { value: e.name || '', placeholder: 'e.g. GitHub (Claude commits)', readonly: !!e.name });
        const env = el('input', { value: e.env || '', placeholder: 'GH_TOKEN' });
        const value = el('input', { type: 'password', autocomplete: 'off',
            placeholder: e.has_value ? 'Stored. Leave blank to keep it.' : 'Token value' });
        const grants = new Set(e.grants || []);
        const boxes = cli.map(a => {
            const cb = el('input', { type: 'checkbox', value: a.name, checked: grants.has(a.name) });
            return { cb, label: el('label', { class: 'agents-toggle' }, cb, el('span', { text: '@' + a.name })) };
        });
        const form = el('form', { class: 'agents-form settings-secret-box' },
            el('strong', { text: e.name ? `Edit ${e.name}` : 'Add a secret' }),
            el('div', { class: 'agents-inline' },
                el('label', { class: 'agents-field' }, el('span', { text: 'Name' }), name),
                el('label', { class: 'agents-field' }, el('span', { text: 'Environment variable' }), env)),
            el('label', { class: 'agents-field' }, el('span', { text: 'Value' }), value),
            el('div', { class: 'agents-field' }, el('span', { text: 'Agents allowed to use it' }),
                cli.length ? el('div', { class: 'agents-inline' }, ...boxes.map(b => b.label))
                           : el('small', { text: 'No agents yet (Settings > Agents).' })),
            el('div', { class: 'agents-actions' },
                e.name ? el('button', { type: 'button', class: 'agents-btn', text: 'Cancel', onclick: () => show('secrets') }) : null,
                el('button', { type: 'submit', class: 'agents-btn primary', text: 'Save secret' })));
        form.addEventListener('submit', async (ev) => {
            ev.preventDefault();
            try {
                await api('POST', '/api/admin/secrets', { name: name.value.trim(), env: env.value.trim(), value: value.value,
                    grants: boxes.filter(b => b.cb.checked).map(b => b.cb.value) });
                show('secrets');
            } catch (err) { alert(err.message); }
        });
        panel.append(form);
        if (secrets.length) panel.append(table(['Name', 'Variable', 'Allowed agents', 'Updated', ''], secrets.map(x => [
            x.name, x.env, (x.grants || []).map(g => '@' + g).join(', ') || 'nobody', x.updated || x.created,
            el('div', { class: 'agents-inline' },
                el('button', { class: 'agents-btn', text: 'Edit', onclick: () => {
                    const p = document.getElementById('settings-panel'); p.textContent = ''; renderSecrets(p, x);
                } }),
                el('button', { class: 'agents-btn danger', text: 'Delete', onclick: async () => {
                    if (!confirm(`Delete secret "${x.name}"? Agents lose ${x.env} from their next turn.`)) return;
                    await api('DELETE', `/api/admin/secrets/${encodeURIComponent(x.name)}`);
                    show('secrets');
                } }))])));
        panel.append(el('p', { class: 'settings-intro', text:
            'Limit: all agents run as the same Linux user on this server. Claude Code agents are blocked from reading AgentHub\'s data folder with their file tools, but an agent with Full access could still find a secret through the shell. Scope tokens narrowly (e.g. a fine-grained GitHub token for one repo).' }));
    }

    // ------------------------------------------------------------ Voice
    async function renderVoice(panel) {
        heading(panel, 'Voice',
            'Speech providers for reading messages aloud and for conversations (any OpenAI-style /audio/speech service), the speech-to-text server, and cloned voices. Each agent picks its provider, voice and speed on its own page under Agents.');
        let conf;
        try { conf = await api('GET', '/api/voice/config'); } catch (err) {
            panel.append(el('p', { class: 'agents-warn', text: err.message })); return;
        }
        // Mic button behaviour: per browser, saved at once
        const typing = el('select', { 'aria-label': 'Voice typing' },
            el('option', { value: 'box', text: 'Type into the message box (you press Send)' }),
            el('option', { value: 'auto', text: 'Type into the box and send when you stop talking' }),
            el('option', { value: 'live', text: 'Show live in the chat and send when you stop talking' }));
        try { typing.value = localStorage.getItem('agenthub-voice-typing') || 'box'; } catch (_) { /* private mode */ }
        typing.addEventListener('change', () => { try { localStorage.setItem('agenthub-voice-typing', typing.value); } catch (_) {} });
        const barge = el('input', { type: 'checkbox' });
        try { barge.checked = JSON.parse(localStorage.getItem('agenthub-voice-bargein') || 'false'); } catch (_) {}
        barge.addEventListener('change', () => { try { localStorage.setItem('agenthub-voice-bargein', JSON.stringify(barge.checked)); } catch (_) {} });
        panel.append(el('label', { class: 'agents-toggle' }, barge,
            el('span', { text: 'Interrupt an agent by talking over it (barge-in). Use with headphones: with speakers the mic hears the agent. Off: the mic pauses while an agent speaks.' })));
        // Voice by name (voice_gate.py): hands-free speech reaches an agent only when said TO it
        const named = el('input', { type: 'checkbox' });
        try { named.checked = JSON.parse(localStorage.getItem('agenthub-voice-named') ?? 'true'); } catch (_) { named.checked = true; }
        named.addEventListener('change', () => { try { localStorage.setItem('agenthub-voice-named', JSON.stringify(named.checked)); } catch (_) {} });
        panel.append(el('label', { class: 'agents-toggle' }, named,
            el('span', { text: 'Answer only when named (no wake word): in hands-free talk, an agent answers only when you say its name anywhere in the sentence, then follow-ups for 30 s without the name. Talking about an agent is ignored. Ignored speech is not posted or kept.' })));
        const check = el('select', { 'aria-label': 'Voice check model' });
        const checkNote = el('small', { text: 'Loading models…' });
        panel.append(el('label', { class: 'agents-field' },
            el('span', { text: 'Voice check model (decides whether a sentence was said to an agent; from Settings > LLMs)' }), check, checkNote));
        (async () => {
            try {
                const [servers, cur] = await Promise.all([api('GET', '/api/admin/llms'), api('GET', '/api/voice/check-model')]);
                check.append(el('option', { value: '', text: 'None (named voice ignores everything)' }));
                const lists = await Promise.all(servers.map(x => api('POST', `/api/admin/llms/${encodeURIComponent(x.id)}/test`)
                    .then(r => r.models).catch(() => [])));
                servers.forEach((x, i) => {
                    let host = x.base_url;
                    try { host = new URL(x.base_url).host; } catch (_) { /* as typed */ }
                    lists[i].forEach(m => check.append(el('option', { value: `${x.id}|${m}`, text: `${x.name} (${host}) · ${m}` })));
                });
                const want = cur.server ? `${cur.server}|${cur.model}` : '';
                if (want && ![...check.options].some(o => o.value === want)) check.append(el('option', { value: want, text: `${cur.server} · ${cur.model} (unreachable)` }));
                check.value = want;
                checkNote.textContent = '';
            } catch (err) { checkNote.textContent = err.message; }
        })();
        check.addEventListener('change', async () => {
            const [server, model] = check.value ? check.value.split('|') : ['', ''];
            try { await api('POST', '/api/voice/check-model', { server, model }); checkNote.textContent = 'Saved'; }
            catch (err) { checkNote.textContent = err.message; }
        });
        panel.append(el('label', { class: 'agents-field' }, el('span', { text: 'Mic button (voice typing, this browser)' }), typing),
            el('h3', { text: 'Speech providers' }));
        const rows = conf.providers.map(p => ({ ...p, parameters: { ...(p.parameters || {}) } }));
        const list = el('div');
        const fieldsOf = [['name', 'Name'], ['base_url', 'Base URL (…/v1)'], ['model', 'Model'], ['format', 'Format'],
            ['voices_url', 'Voices list URL'], ['clone_url', 'Voice upload URL (cloning)'], ['default_voice', 'Default voice'],
            ['sample_rate', 'Sample rate (streaming)'], ['capabilities_url', 'Parameters URL (blank: the service\'s openapi.json)']];
        // Engine parameters: each service says which settings it takes; controls are drawn from that
        const caps = {};  // provider name -> promise of [{name, type, min, max, ...}]
        const paramControl = (spec, values) => {
            const set = (v) => { if (v === '' || v == null) delete values[spec.name]; else values[spec.name] = v; };
            const dflt = spec.default == null ? '' : ` (${spec.default})`;
            let input;
            if (spec.enum || spec.type === 'boolean') {
                const opts = spec.enum ? spec.enum.map(String) : ['true', 'false'];
                input = el('select', {}, el('option', { value: '', text: `Engine default${dflt}` }),
                    ...opts.map(o => el('option', { value: o, text: spec.enum ? o : (o === 'true' ? 'On' : 'Off') })));
                input.value = values[spec.name] == null ? '' : String(values[spec.name]);
                input.addEventListener('change', () => set(input.value));
            } else if (spec.type !== 'string' && spec.min != null && spec.max != null) {
                const out = el('output');
                const show = () => { out.textContent = values[spec.name] == null ? `default${dflt}` : String(values[spec.name]); };
                const range = el('input', { type: 'range', min: spec.min, max: spec.max,
                    step: spec.step || (spec.type === 'integer' ? 1 : 0.01), 'aria-label': spec.name });
                range.value = values[spec.name] ?? spec.default ?? spec.min;
                range.addEventListener('input', () => { set(Number(range.value)); show(); });
                show();
                input = el('div', { class: 'voice-param-range' }, range, out);
            } else {
                input = el('input', { type: spec.type === 'string' ? 'text' : 'number', step: spec.type === 'integer' ? 1 : 'any',
                    min: spec.min, max: spec.max, value: values[spec.name] ?? '', placeholder: `Engine default${dflt}` });
                input.addEventListener('input', () => set(input.value));
            }
            return el('label', { class: 'agents-field' }, el('span', { text: spec.name }), input,
                spec.description ? el('small', { text: spec.description }) : null);
        };
        const drawParams = async (box, r) => {
            box.textContent = '';
            if (!conf.providers.some(p => p.name === r.name)) return;  // not saved yet: nothing to ask
            let specs;
            try {
                caps[r.name] = caps[r.name] || api('GET', '/api/voice/capabilities?provider=' + encodeURIComponent(r.name));
                specs = (await caps[r.name]).parameters;
            } catch (_) { return; }  // the service lists no parameters: nothing to set
            if (!specs.length) return;
            r.parameters = r.parameters || {};
            const grid = (list) => el('div', { class: 'voice-params' }, ...list.map(sp => paramControl(sp, r.parameters)));
            const adv = specs.filter(sp => sp.advanced);
            box.append(el('strong', { text: 'Engine parameters' }),
                el('small', { text: ' Sent with every request to this provider. Unset values use the engine\'s own default.' }),
                grid(specs.filter(sp => !sp.advanced)),
                adv.length ? el('details', {}, el('summary', { text: 'Advanced' }), grid(adv)) : '',
                el('button', { type: 'button', class: 'agents-btn', text: 'Reset to defaults',
                    onclick: () => { r.parameters = {}; drawParams(box, r); } }));
        };
        // A dropdown of known values; a saved value that is not in the list stays selectable
        const choice = (value, opts, blank, label, onpick) => {
            const cur = value == null ? '' : String(value);
            const all = cur && !opts.includes(cur) ? [cur, ...opts] : opts;
            const sel = el('select', { 'aria-label': label }, ...(blank ? [el('option', { value: '', text: blank })] : []),
                ...all.map(o => el('option', { value: o, text: o })));
            sel.value = cur;
            sel.addEventListener('change', () => onpick(sel.value));
            return sel;
        };
        const FORMATS = ['mp3', 'wav', 'opus', 'flac', 'aac', 'pcm'];
        const RATES = ['16000', '22050', '24000', '44100', '48000'];
        const def = el('select', { 'aria-label': 'Default provider' });
        const draw = () => {
            list.textContent = '';
            rows.forEach((r, i) => {
                const box = el('fieldset', { class: 'agents-group' }, el('legend', { text: r.name || 'New provider' }));
                const grid = el('div', { class: 'agents-inline' });
                const known = conf.voices[r.name] || {};
                const lists = { format: [FORMATS, ''], sample_rate: [RATES, 'As sent by the service'],
                    default_voice: [known.voices || [], 'None'], model: [known.models || [], 'None'] };
                for (const [k, label] of fieldsOf) {
                    const [opts, blank] = lists[k] || [[], ''];
                    let inp;
                    if (opts.length) {  // the service (or the format list) says what can be chosen
                        inp = choice(r[k] || (k === 'format' ? 'mp3' : ''), opts, blank, label, (v) => { r[k] = v; });
                    } else {            // URLs and names; also voice and model while the service cannot be asked
                        inp = el('input', { value: r[k] || '', 'aria-label': label });
                        inp.addEventListener('input', () => { r[k] = inp.value; });
                    }
                    grid.append(el('label', { class: 'agents-field' }, el('span', { text: label }), inp));
                }
                const st = el('input', { type: 'checkbox', checked: !!r.stream });
                st.addEventListener('change', () => { r.stream = st.checked; });
                grid.append(el('label', { class: 'agents-toggle' }, st, el('span', { text: 'Streams raw PCM (starts speaking on the first chunk)' })));
                const key = el('input', { type: 'password', placeholder: r.has_key ? 'stored (leave blank to keep)' : 'none', autocomplete: 'off' });
                key.addEventListener('input', () => { r.api_key = key.value; });
                grid.append(el('label', { class: 'agents-field' }, el('span', { text: 'API key' }), key));
                const err = (conf.voices[r.name] || {}).error;
                const count = (conf.voices[r.name] || {}).voices?.length;
                const params = el('div');
                drawParams(params, r);
                box.append(grid, params, el('small', { class: err ? 'agents-warn' : '', text: err ? `Unreachable: ${err}` : count != null ? `${count} voices` : '' }),
                    el('button', { type: 'button', class: 'agents-btn danger', text: 'Remove', onclick: () => { rows.splice(i, 1); draw(); } }));
                list.append(box);
            });
            def.textContent = '';
            def.append(...rows.filter(r => r.name).map(r => el('option', { value: r.name, text: r.name })));
            def.value = conf.default_provider;
        };
        draw();
        const stt = el('input', { value: conf.stt.wyoming, placeholder: 'host:port' });
        const langName = new Intl.DisplayNames(['en'], { type: 'language' });
        const lang = el('select', { 'aria-label': 'Language' }, ...[...new Set([conf.stt.language || 'en', 'en', 'es', 'fr', 'de', 'it',
            'pt', 'nl', 'sv', 'da', 'no', 'fi', 'pl', 'cs', 'uk', 'ru', 'tr', 'ar', 'he', 'hi', 'ja', 'ko', 'zh'])]
            .map(c => el('option', { value: c, text: `${langName.of(c)} (${c})` })));
        lang.value = conf.stt.language || 'en';
        const cacheMb = el('input', { type: 'number', min: '0', max: '100000', value: conf.cache_mb ?? 512 });
        const save = el('button', { type: 'button', class: 'agents-btn primary', text: 'Save voice settings' });
        save.onclick = async () => {
            try {
                await api('POST', '/api/voice/config', { providers: rows, default_provider: def.value, cache_mb: cacheMb.value,
                    stt: { wyoming: stt.value.trim(), language: lang.value.trim() } });
                show('voice');
            } catch (err) { alert(err.message); }
        };
        panel.append(list,
            el('button', { type: 'button', class: 'agents-btn', text: '+ Add provider', onclick: () => {
                rows.push({ name: '', base_url: '', format: 'mp3' }); draw(); } }),
            el('div', { class: 'agents-inline' },
                el('label', { class: 'agents-field' }, el('span', { text: 'Default provider' }), def),
                el('label', { class: 'agents-field' }, el('span', { text: 'Speech-to-text (Wyoming Whisper)' }), stt),
                el('label', { class: 'agents-field' }, el('span', { text: 'Language' }), lang),
                el('label', { class: 'agents-field' }, el('span', { text: 'Saved audio (MB, 0 = off)' }), cacheMb,
                    el('small', { text: 'Sentences read aloud are kept on the server, so a replay plays the same audio at once. The oldest are removed when this is full.' }))),
            el('div', { class: 'agents-actions' }, save));

        // Clone a voice from a short sample
        const cloneable = conf.providers.filter(p => (conf.voices[p.name] || {}).clone);
        if (!cloneable.length) return;
        const cp = el('select', { 'aria-label': 'Provider' }, ...cloneable.map(p => el('option', { value: p.name, text: p.name })));
        const cn = el('input', { placeholder: 'e.g. Narrator' });
        const cf = el('input', { type: 'file', accept: 'audio/*' });
        const go = el('button', { type: 'button', class: 'agents-btn primary', text: 'Clone voice' });
        const note = el('small', { role: 'status' });
        go.onclick = async () => {
            if (!cf.files[0] || !cn.value.trim()) { note.textContent = 'Give the voice a name and pick a sample.'; return; }
            const form = new FormData();
            form.append('provider', cp.value); form.append('name', cn.value.trim()); form.append('file', cf.files[0]);
            go.disabled = true; note.textContent = 'Uploading…';
            try {
                const res = await fetch('/api/voice/clone', { method: 'POST', headers: { 'X-Session-Token': window.SESSION_TOKEN }, body: form });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || 'cloning failed');
                note.textContent = `Added "${data.name}". Pick it on an agent's page.`;
            } catch (err) { note.textContent = err.message; } finally { go.disabled = false; }
        };
        panel.append(el('h3', { text: 'Clone a voice' }),
            el('p', { class: 'agents-sub', text: 'Upload 10 to 30 seconds of one person speaking clearly, without music or background noise. Only clone voices you have permission to use.' }),
            el('div', { class: 'agents-inline' },
                el('label', { class: 'agents-field' }, el('span', { text: 'Provider' }), cp),
                el('label', { class: 'agents-field' }, el('span', { text: 'Voice name' }), cn),
                el('label', { class: 'agents-field' }, el('span', { text: 'Sample' }), cf), go),
            note);
    }

    // ------------------------------------------------------------ Rulebooks
    async function renderRulebooks(panel, editing) {
        heading(panel, 'Rulebooks',
            'Guides for kinds of work: story writing, code & processes, docs. Agents open one only when you ask ("use the story rulebook"), so unused rulebooks cost no context. Attach one to channels or agents to have it followed every turn. Ask for a review with: /review <rulebook> @agent <text or file path>.');
        const [books, agents, access] = await Promise.all([api('GET', '/api/admin/rulebooks'), api('GET', '/api/managed-agents'), api('GET', '/api/admin/rulebook-access')]);
        const fine = access.fine_grained;
        const channels = (typeof channelList !== 'undefined' && channelList.length) ? channelList : ['general'];
        const e = editing || null;
        if (e) {
            const name = el('input', { value: e.name || '', placeholder: 'e.g. Story writing' });
            const desc = el('input', { value: e.description || '', placeholder: 'One line: what it is for' });
            const text = el('textarea', { rows: 14, placeholder: 'The rules, one per line or in short paragraphs.' });
            text.value = e.text || '';
            const pick = (items, chosen, prefix) => items.map(v => {
                const cb = el('input', { type: 'checkbox', value: v, checked: (chosen || []).includes(v) });
                return { cb, label: el('label', { class: 'agents-toggle' }, cb, el('span', { text: prefix + v })) };
            });
            const chans = pick(channels, e.channels, '#');
            const ags = pick(agents.map(a => a.name), e.agents, '@');
            const rds = pick(agents.map(a => a.name), e.readers, '@');
            const form = el('form', { class: 'agents-form' },
                el('h2', { text: e.name ? `Edit "${e.name}"` : 'New rulebook' }),
                el('div', { class: 'agents-inline' },
                    el('label', { class: 'agents-field' }, el('span', { text: 'Name' }), name),
                    el('label', { class: 'agents-field' }, el('span', { text: 'Description' }), desc)),
                el('label', { class: 'agents-field' }, el('span', { text: 'Rules' }), text),
                el('div', { class: 'agents-field' }, el('span', { text: 'Follow it in these channels' }),
                    el('div', { class: 'agents-inline' }, ...chans.map(x => x.label))),
                el('div', { class: 'agents-field' }, el('span', { text: 'Agents that always follow it' }),
                    el('div', { class: 'agents-inline' }, ...ags.map(x => x.label))),
                fine ? el('div', { class: 'agents-field' }, el('span', { text: 'Agents that can open it on request (fine-grained access)' }),
                    el('div', { class: 'agents-inline' }, ...rds.map(x => x.label))) : '',
                el('div', { class: 'agents-actions' },
                    el('button', { type: 'button', class: 'agents-btn', text: 'Cancel', onclick: () => show('rulebooks') }),
                    el('button', { type: 'submit', class: 'agents-btn primary', text: 'Save rulebook' })));
            form.addEventListener('submit', async (ev) => {
                ev.preventDefault();
                try {
                    await api('POST', '/api/admin/rulebooks', { original: e.name || null, name: name.value.trim(),
                        description: desc.value.trim(), text: text.value,
                        channels: chans.filter(x => x.cb.checked).map(x => x.cb.value),
                        agents: ags.filter(x => x.cb.checked).map(x => x.cb.value),
                        readers: rds.filter(x => x.cb.checked).map(x => x.cb.value) });
                    show('rulebooks');
                } catch (err) { alert(err.message); }
            });
            panel.textContent = '';
            panel.append(form);
            name.focus();
            return;
        }
        panel.append(el('button', { class: 'agents-btn primary', text: '+ New rulebook', onclick: () => {
            const p = document.getElementById('settings-panel'); renderRulebooks(p, {});
        } }));
        const fineBox = el('input', { type: 'checkbox', checked: fine, onchange: async () => {
            try { await api('POST', '/api/admin/rulebook-access', { fine_grained: fineBox.checked }); show('rulebooks'); }
            catch (err) { alert(err.message); }
        } });
        panel.append(el('details', { class: 'agents-field', open: fine },
            el('summary', { text: 'Advanced' }),
            el('label', { class: 'agents-toggle' }, fineBox,
                el('span', { text: 'Fine-grained access: agents can open only rulebooks attached to them, their channel, or listing them as a reader. Off: every agent can open every rulebook.' }))));
        panel.append(table(['Rulebook', 'Applies to', ''], books.map(b => [
            el('div', {}, el('strong', { text: b.name }), el('div', { class: 'agents-sub', text: b.description || '' })),
            ([...(b.channels || []).map(c => '#' + c), ...(b.agents || []).map(a => '@' + a)].join(' ') || 'on request only')
                + (fine ? ' · can open: ' + ((b.readers || []).map(a => '@' + a).join(' ') || 'attached only') : ''),
            el('div', { class: 'agents-inline' },
                el('button', { class: 'agents-btn', text: 'Edit', onclick: () => {
                    const p = document.getElementById('settings-panel'); renderRulebooks(p, b);
                } }),
                el('button', { class: 'agents-btn danger', text: 'Delete', onclick: async () => {
                    if (!confirm(`Delete rulebook "${b.name}"?`)) return;
                    await api('DELETE', `/api/admin/rulebooks/${encodeURIComponent(b.name)}`);
                    show('rulebooks');
                } }))])));
    }

    // ------------------------------------------------------------ LLM servers
    async function renderLlms(panel, editing) {
        heading(panel, 'LLMs',
            'The LLM servers your API agents use. Each server is set up once, with its key and a default model; agents pick a server here and use its default or one of its models. A server that offers a single model uses it as the default. Keys are write-only.');
        const servers = await api('GET', '/api/admin/llms');
        const presets = (window.AgentsSection && window.AgentsSection.PROVIDERS) || {};
        const e = editing || { api_format: 'openai' };
        const preset = el('select', { 'aria-label': 'Start from' });
        preset.append(el('option', { value: '', text: 'Start from…' }));
        for (const [k, p] of Object.entries(presets)) preset.append(el('option', { value: k, text: p.label }));
        const name = el('input', { value: e.name || '', placeholder: 'e.g. Strata (local)' });
        const baseUrl = el('input', { value: e.base_url || '', placeholder: 'http://127.0.0.1:4000/v1' });
        const format = el('select');
        format.append(el('option', { value: 'openai', text: 'OpenAI-compatible' }), el('option', { value: 'anthropic', text: 'Anthropic' }));
        format.value = e.api_format || 'openai';
        const key = el('input', { type: 'password', autocomplete: 'off',
            placeholder: e.has_api_key ? 'Stored. Leave blank to keep it.' : 'API key (blank for local servers)' });
        const def = el('select', { 'aria-label': 'Default model' });
        const defNote = el('small');
        const fillDefaults = (models, current) => {
            def.textContent = '';
            def.append(el('option', { value: '', text: 'None (agents must pin a model)' }));
            const all = current && !models.includes(current) ? [current, ...models] : models;
            all.forEach(m => def.append(el('option', { value: m, text: m })));
            def.value = current || '';
        };
        fillDefaults([], e.default_model || '');
        if (e.id) api('POST', `/api/admin/llms/${encodeURIComponent(e.id)}/test`)
            .then(r => { fillDefaults(r.models, r.default_model || e.default_model || ''); defNote.textContent = `${r.models.length} models`; })
            .catch(err => { defNote.textContent = err.message; });
        else defNote.textContent = 'Save the server, then edit it to pick from its models.';
        preset.addEventListener('change', () => {
            const p = presets[preset.value];
            if (!p) return;
            if (p.base_url) baseUrl.value = p.base_url;
            format.value = p.api_format;
            if (!name.value) name.value = p.label;
        });
        const form = el('form', { class: 'agents-form settings-secret-box' },
            el('strong', { text: e.id ? `Edit ${e.name}` : 'Add an LLM server' }),
            e.id ? null : el('label', { class: 'agents-field' }, el('span', { text: 'Start from' }), preset),
            el('div', { class: 'agents-inline' },
                el('label', { class: 'agents-field' }, el('span', { text: 'Name' }), name),
                el('label', { class: 'agents-field' }, el('span', { text: 'Base URL' }), baseUrl),
                el('label', { class: 'agents-field' }, el('span', { text: 'API format' }), format)),
            el('label', { class: 'agents-field' }, el('span', { text: 'API key (write-only; changing the host without a new key clears the stored one)' }), key),
            el('label', { class: 'agents-field' }, el('span', { text: 'Default model' }), def, defNote),
            el('div', { class: 'agents-actions' },
                e.id ? el('button', { type: 'button', class: 'agents-btn', text: 'Cancel', onclick: () => show('llms') }) : null,
                el('button', { type: 'submit', class: 'agents-btn primary', text: 'Save server' })));
        form.addEventListener('submit', async (ev) => {
            ev.preventDefault();
            const body = { id: e.id || '', name: name.value.trim(), base_url: baseUrl.value.trim(),
                api_format: format.value, api_key: key.value };
            // only when changed: an untouched default keeps following a one-model server
            if (e.id && def.value !== (e.default_model || '')) body.default_model = def.value;
            try { await api('POST', '/api/admin/llms', body); show('llms'); }
            catch (err) { alert(err.message); }
        });
        panel.append(form);
        if (!servers.length) return;
        panel.append(table(['Name', 'Host', 'Format', 'Default model', 'Models', 'Agents', ''], servers.map(x => {
            const count = el('span', { text: 'checking…' });
            const defCell = el('span', { text: x.default_model || 'none' });
            api('POST', `/api/admin/llms/${encodeURIComponent(x.id)}/test`)
                .then(r => { count.textContent = String(r.models.length); defCell.textContent = r.default_model || 'none'; })
                .catch(() => { count.textContent = 'unreachable'; });
            let host = x.base_url;
            try { host = new URL(x.base_url).host; } catch (_) { /* keep as typed */ }
            const agents = (x.agents || []).map(n => '@' + n).join(', ') || '—';
            const actions = x.readonly ? el('small', { text: 'from config.toml' }) : el('div', { class: 'agents-inline' },
                el('button', { class: 'agents-btn', text: 'Edit', onclick: () => {
                    const p = document.getElementById('settings-panel'); p.textContent = ''; renderLlms(p, x);
                } }),
                (x.agents || []).length ? el('button', { class: 'agents-btn', text: 'Unlink agents', onclick: async () => {
                    if (!confirm(`Unlink ${x.agents.length} agents from ${x.name}? They go offline until you edit them and pick a server.`)) return;
                    try { await api('POST', `/api/admin/llms/${encodeURIComponent(x.id)}/unlink`); show('llms'); }
                    catch (err) { alert(err.message); }
                } }) : null,
                el('button', { class: 'agents-btn danger', text: (x.agents || []).length ? `Remove (${x.agents.length} linked)` : 'Remove',
                    disabled: !!(x.agents || []).length, onclick: async () => {
                        if (!confirm(`Remove LLM server "${x.name}"?`)) return;
                        try { await api('DELETE', `/api/admin/llms/${encodeURIComponent(x.id)}`); show('llms'); }
                        catch (err) { alert(err.message); }
                    } }));
            return [x.name, host, x.api_format === 'anthropic' ? 'Anthropic' : 'OpenAI', defCell, count, agents, actions];
        })));
    }

    // ------------------------------------------------------------ MCP servers
    async function renderMcp(panel, editing) {
        heading(panel, 'MCP servers',
            'Tool servers for your agents. Add one, tick the agents that may use it. API agents (Qwen, OpenRouter, ...) get its tools through AgentHub; Claude Code agents get it in their MCP config. Antigravity keeps its own global MCP settings.');
        const [servers, agents] = await Promise.all([api('GET', '/api/admin/mcp'), api('GET', '/api/managed-agents')]);
        const e = editing || { transport: 'http', enabled: true };
        const name = el('input', { value: e.name || '', placeholder: 'e.g. netlab', readonly: !!e.name });
        const transport = el('select');
        transport.append(el('option', { value: 'http', text: 'HTTP (URL)' }), el('option', { value: 'stdio', text: 'Local command (stdio)' }));
        transport.value = e.transport || 'http';
        const url = el('input', { value: e.url || '', placeholder: 'http://192.168.1.50:8097/mcp' });
        const headers = el('textarea', { rows: 2, placeholder: (e.header_names || []).length
            ? (e.header_names.map(h => h + ':  (stored; leave the value blank to keep it)').join('\n'))
            : 'Authorization: Bearer ...  (one per line, optional)' });
        headers.value = (e.header_names || []).map(h => h + ': ').join('\n');
        const command = el('input', { value: e.command || '', placeholder: 'npx' });
        const args = el('input', { value: (e.args || []).join(' '), placeholder: '-y @modelcontextprotocol/server-filesystem /data/projects' });
        const env = el('textarea', { rows: 2, placeholder: 'KEY=value (one per line, optional)' });
        env.value = (e.env_names || []).map(k => k + '=').join('\n');
        const httpBox = el('div', { class: 'agents-group' },
            el('label', { class: 'agents-field' }, el('span', { text: 'URL' }), url),
            el('label', { class: 'agents-field' }, el('span', { text: 'Headers (values are write-only)' }), headers));
        const stdioBox = el('div', { class: 'agents-group' },
            el('div', { class: 'agents-inline' },
                el('label', { class: 'agents-field' }, el('span', { text: 'Command' }), command),
                el('label', { class: 'agents-field' }, el('span', { text: 'Arguments (space-separated)' }), args)),
            el('label', { class: 'agents-field' }, el('span', { text: 'Environment (values are write-only)' }), env));
        const timeout = el('input', { type: 'number', min: '1', max: '3600', value: e.timeout || '', placeholder: '120' });
        const sync = () => { httpBox.hidden = transport.value !== 'http'; stdioBox.hidden = transport.value !== 'stdio'; };
        transport.addEventListener('change', sync);
        sync();
        const grants = new Set(e.grants || []);
        const boxes = agents.filter(a => a.kind !== 'agy').map(a => {
            const cb = el('input', { type: 'checkbox', value: a.name, checked: grants.has(a.name) });
            return { cb, label: el('label', { class: 'agents-toggle' }, cb, el('span', { text: '@' + a.name })) };
        });
        const form = el('form', { class: 'agents-form settings-secret-box' },
            el('strong', { text: e.name ? `Edit ${e.name}` : 'Add an MCP server' }),
            el('div', { class: 'agents-inline' },
                el('label', { class: 'agents-field' }, el('span', { text: 'Name' }), name),
                el('label', { class: 'agents-field' }, el('span', { text: 'Type' }), transport),
                el('label', { class: 'agents-field' }, el('span', { text: 'Tool timeout (seconds)' }), timeout)),
            httpBox, stdioBox,
            el('div', { class: 'agents-field' }, el('span', { text: 'Agents allowed to use it' }),
                boxes.length ? el('div', { class: 'agents-inline' }, ...boxes.map(b => b.label))
                             : el('small', { text: 'No Claude Code or API agents yet (Settings > Agents).' })),
            el('div', { class: 'agents-actions' },
                e.name ? el('button', { type: 'button', class: 'agents-btn', text: 'Cancel', onclick: () => show('mcp') }) : null,
                el('button', { type: 'submit', class: 'agents-btn primary', text: 'Save server' })));
        form.addEventListener('submit', async (ev) => {
            ev.preventDefault();
            try {
                await api('POST', '/api/admin/mcp', { name: name.value.trim(), transport: transport.value,
                    url: url.value.trim(), headers: headers.value, command: command.value.trim(),
                    args: args.value.trim() ? args.value.trim().split(/\s+/) : [], env: env.value,
                    grants: boxes.filter(b => b.cb.checked).map(b => b.cb.value), enabled: true,
                    timeout: timeout.value ? Number(timeout.value) : null });
                show('mcp');
            } catch (err) { alert(err.message); }
        });
        panel.append(form);
        // OAuth sign-in: the provider only redirects to 127.0.0.1, so the browser lands on a
        // dead page whose address holds the code; the user pastes that address back here.
        const oauthSlot = el('div');
        panel.append(oauthSlot);
        async function oauthBox(x) {
            oauthSlot.textContent = '';
            const status = el('small', { text: 'Contacting the server…' });
            const box = el('div', { class: 'agents-form settings-secret-box' },
                el('strong', { text: `Sign in to ${x.name}` }), status);
            oauthSlot.append(box);
            box.scrollIntoView({ block: 'nearest' });
            let url;
            try { url = (await api('POST', `/api/admin/mcp/${encodeURIComponent(x.name)}/oauth/start`)).url; }
            catch (err) { status.textContent = err.message; return; }
            const landed = el('input', { placeholder: 'http://127.0.0.1:8765/callback?code=…&state=…',
                'aria-label': 'Address the browser landed on' });
            const done = el('button', { type: 'submit', class: 'agents-btn primary', text: 'Finish sign-in' });
            const step2 = el('form', { class: 'agents-field' },
                el('span', { text: '2. After approving, the new tab shows "can\'t connect" at 127.0.0.1. Copy that whole address and paste it here:' }),
                landed, el('div', { class: 'agents-actions' },
                    el('button', { type: 'button', class: 'agents-btn', text: 'Cancel', onclick: () => { oauthSlot.textContent = ''; } }),
                    done));
            step2.addEventListener('submit', async (ev) => {
                ev.preventDefault();
                done.disabled = true;
                try {
                    const r = await api('POST', `/api/admin/mcp/${encodeURIComponent(x.name)}/oauth/finish`, { url: landed.value.trim() });
                    alert(r.message);
                    show('mcp');
                } catch (err) { status.textContent = err.message; done.disabled = false; }
            });
            status.textContent = '';
            status.append('1. ', el('a', { href: url, target: '_blank', rel: 'noopener noreferrer',
                text: 'Open the sign-in page in a new tab' }), ' and approve access.');
            box.append(step2);
        }
        if (servers.length) panel.append(table(['Name', 'Where', 'Allowed agents', 'Tools', ''], servers.map(x => {
            const tools = el('span', { text: '—' });
            return [x.name, x.transport === 'stdio' ? `${x.command} ${(x.args || []).join(' ')}` : x.url,
                (x.grants || []).map(g => '@' + g).join(', ') || 'nobody', tools,
                el('div', { class: 'agents-inline' },
                    el('button', { class: 'agents-btn', text: 'Test', onclick: async () => {
                        tools.textContent = 'connecting…';
                        try {
                            const r = await api('POST', `/api/admin/mcp/${encodeURIComponent(x.name)}/test`);
                            tools.textContent = `${r.tools.length}: ${r.tools.slice(0, 8).join(', ')}${r.tools.length > 8 ? ', …' : ''}`;
                        } catch (err) { tools.textContent = err.message; }
                    } }),
                    x.transport === 'stdio' ? null : el('button', { class: 'agents-btn',
                        text: x.oauth ? 'Sign in again' : 'Sign in (OAuth)', onclick: () => oauthBox(x) }),
                    el('button', { class: 'agents-btn', text: 'Edit', onclick: () => {
                        const p = document.getElementById('settings-panel'); p.textContent = ''; renderMcp(p, x);
                    } }),
                    el('button', { class: 'agents-btn danger', text: 'Delete', onclick: async () => {
                        if (!confirm(`Delete MCP server "${x.name}"?`)) return;
                        await api('DELETE', `/api/admin/mcp/${encodeURIComponent(x.name)}`);
                        show('mcp');
                    } }))];
        })));
    }

    // ------------------------------------------------------------ Metrics
    async function renderMetrics(panel, hours = 24) {
        heading(panel, 'Metrics', 'Every model call by API agents: tokens, prompt cache hits, speed and cost. '
            + 'Speed is output tokens over the whole call, including the time the model spends reading the prompt.');
        const pick = el('select', { 'aria-label': 'Period', onchange: () => { panel.textContent = ''; renderMetrics(panel, +pick.value); } });
        [[1, 'Last hour'], [24, 'Last 24 hours'], [168, 'Last 7 days'], [720, 'Last 30 days']].forEach(([h, t]) =>
            pick.append(el('option', { value: String(h), text: t, selected: h === hours })));
        panel.append(el('div', { class: 'agents-inline' }, pick,
            el('button', { class: 'agents-btn', text: 'Refresh', onclick: () => { panel.textContent = ''; renderMetrics(panel, hours); } })));
        const m = await api('GET', `/api/admin/metrics?hours=${hours}`);
        const n = (x) => (x == null ? '—' : Number(x).toLocaleString());
        if (!m.rows.length) {
            panel.append(el('p', { text: 'No model calls recorded in this period.' }));
            return;
        }
        panel.append(table(['Agent', 'Model', 'Calls', 'Prompt tokens', 'Cached', 'Cache hit', 'Output tokens', 'Tokens/s', 'Avg call', 'Cost'],
            m.rows.map(r => [r.agent, r.model, n(r.calls), n(r.prompt_tokens), n(r.cached_tokens), `${r.cache_hit_pct}%`,
                n(r.completion_tokens), r.tokens_per_s == null ? '—' : String(r.tokens_per_s), `${r.avg_seconds} s`,
                r.cost == null ? '—' : `$${r.cost.toFixed(4)}`])));
        panel.append(el('h3', { text: 'Latest calls' }));
        panel.append(table(['Time', 'Agent', 'Channel', 'Prompt', 'Cached', 'Output', 'Seconds', 'Tokens/s', 'Cost'],
            m.recent.map(r => [new Date(r.ts * 1000).toLocaleTimeString(), r.agent, r.channel ? '#' + r.channel : '',
                n(r.prompt_tokens), n(r.cached_tokens), n(r.completion_tokens), String(r.seconds),
                r.tokens_per_s == null ? '—' : String(r.tokens_per_s), r.cost == null ? '—' : `$${Number(r.cost).toFixed(4)}`])));
    }

    // ------------------------------------------------------------ IRC
    async function renderIrc(panel) {
        heading(panel, 'IRC', 'Use any IRC client (Halloy, mIRC, AdiIRC, Goguma, The Lounge). Channels are #channels, agents are nicks; a working agent shows as away. Plaintext: trusted network only.');
        const irc = await api('GET', '/api/admin/irc');
        if (!irc.enabled) {
            panel.append(el('p', { text: 'The IRC gateway is off. Enable it under [irc] in config.local.toml and restart.' }));
            return;
        }
        const pw = el('input', { type: 'password', readonly: true, value: irc.password, class: 'settings-secret' });
        const reveal = el('button', { class: 'agents-btn', text: 'Show', onclick: () => {
            pw.type = pw.type === 'password' ? 'text' : 'password';
            reveal.textContent = pw.type === 'password' ? 'Show' : 'Hide';
        } });
        panel.append(table(['Setting', 'Value'], [
            ['Server', irc.host], ['Port', String(irc.port)], ['TLS', 'off'],
            ['Server password', el('div', { class: 'agents-inline' }, pw, reveal)],
        ]));
        panel.append(el('button', { class: 'agents-btn', text: 'Generate new password', onclick: async () => {
            if (!confirm('Generate a new IRC password? Connected clients keep working until they reconnect.')) return;
            await api('POST', '/api/admin/irc/password', {});
            show('irc');
        } }));
    }

    window.toggleSettings = open;  // gear icon (inline onclick) now opens this window
    window.openSettings = open;
})();
