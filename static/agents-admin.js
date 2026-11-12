// Agents manager: create, edit, enable/disable and delete room agents that the
// server runs itself (agent_runner.py). Everything is built with createElement /
// textContent. API keys are write-only: the server never sends them back.

(function () {
    const PROVIDERS = {
        strata:     { label: 'Strata (local)',  base_url: 'http://127.0.0.1:8090/v1', api_format: 'openai' },
        openrouter: { label: 'OpenRouter',      base_url: 'https://openrouter.ai/api/v1', api_format: 'openai',
                      help: 'Paste a key from openrouter.ai/keys, then pick a model from the list (type to filter, e.g. "claude" or "free").' },
        gemini:     { label: 'Google Gemini',   base_url: 'https://generativelanguage.googleapis.com/v1beta/openai', api_format: 'openai' },
        anthropic:  { label: 'Anthropic',       base_url: 'https://api.anthropic.com/v1', api_format: 'anthropic' },
        custom:     { label: 'Custom (OpenAI-compatible)', base_url: '', api_format: 'openai' },
    };
    const KINDS = { api: 'LLM API (OpenRouter, Strata, Gemini, Anthropic, OpenAI-compatible)', claude: 'Claude Code (CLI)', agy: 'Antigravity (CLI)' };
    const PERMS = {
        read: 'Read-only',
        edit: 'Can edit files',
        full: 'Full access (no approvals)',
    };

    let agents = [];

    function el(tag, attrs = {}, ...children) {
        const e = document.createElement(tag);
        for (const [k, v] of Object.entries(attrs)) {
            if (k === 'class') e.className = v;
            else if (k === 'text') e.textContent = v;
            else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
            else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? '' : v);
        }
        for (const c of children) if (c) e.append(c);
        return e;
    }

    async function api(method, path, body) {
        const res = await fetch(path, {
            method,
            headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
            body: body ? JSON.stringify(body) : undefined,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        return data;
    }

    let root = null;  // the settings panel this section renders into
    function dialog() { return root; }

    async function mount(container) {
        root = container;
        try {
            agents = await api('GET', '/api/managed-agents');
        } catch (e) {
            agents = [];
        }
        renderList();
    }

    function permSelect(a) {
        const sel = el('select', { class: 'agents-perm', 'aria-label': `Permissions for ${a.name}` });
        for (const [v, label] of Object.entries(PERMS)) sel.append(el('option', { value: v, text: label }));
        sel.value = a.permissions || 'read';
        sel.dataset.perm = sel.value;
        sel.addEventListener('change', async () => {
            if (sel.value === 'full' && !confirm(
                `Give ${a.name} full access?\n\nIt will run any command and edit any file on this machine ` +
                `without asking, and anyone who can post in this room can tell it what to do.`)) {
                sel.value = a.permissions || 'read';
                return;
            }
            await save({ ...a, permissions: sel.value });
        });
        return sel;
    }

    function renderList() {
        const d = dialog();
        d.textContent = '';
        const head = el('div', { class: 'agents-head' },
            el('h2', { text: 'Room agents' }),
            el('button', { class: 'agents-btn primary', text: '+ Add agent', onclick: () => renderForm({}) }));
        d.append(head);
        if (!agents.length) {
            d.append(el('p', { class: 'agents-empty',
                text: 'No room agents yet. Add one: an LLM (OpenRouter, Gemini, Anthropic, local Strata) or a coding agent (Claude Code, Antigravity) that runs here, headless.' }));
            return;
        }
        const list = el('div', { class: 'agents-list' });
        for (const a of agents) {
            const on = el('input', { type: 'checkbox', checked: a.enabled !== false, 'aria-label': `Enable ${a.name}` });
            on.addEventListener('change', () => save({ ...a, enabled: on.checked }));
            const row = el('div', { class: 'agents-row' },
                el('span', { class: 'agents-swatch', style: `background:${a.color || '#8b5cf6'}` }),
                el('div', { class: 'agents-info' },
                    el('div', { class: 'agents-name', text: `@${a.name}` }),
                    el('div', { class: 'agents-sub', text: (a.kind === 'api' ? `${a.model || '?'} · ${providerLabel(a)}` : `${KINDS[a.kind]} · ${a.cwd || '~'}`)
                        + ' · #general' + (a.channels || []).map(c => ' #' + c).join('') }),
                    el('div', { class: 'agents-health', 'data-agent': a.name, text: 'Checking…' })),
                permSelect(a),
                el('label', { class: 'agents-toggle' }, on, el('span', { text: 'On' })),
                el('button', { class: 'agents-btn', text: 'Edit', onclick: () => renderForm(a) }),
                moreMenu(a));
            list.append(row);
        }
        d.append(list);
        loadHealth(list);
    }

    function ago(t) {
        const s = Math.max(0, Math.round(Date.now() / 1000 - t));
        return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`;
    }

    // Health line per agent: endpoint up/down, busy and queued, last success, last error
    async function loadHealth(list) {
        let rows = [];
        try { rows = await api('GET', '/api/managed-agents/health'); } catch { /* leave "Checking…" */ return; }
        const by = Object.fromEntries(rows.map(h => [h.name, h]));
        list.querySelectorAll('.agents-health').forEach(node => {
            const h = by[node.dataset.agent];
            node.textContent = '';
            if (!h) { node.append(el('span', { class: 'agents-health-dot off' }), 'Not running'); return; }
            const ep = h.endpoint || {};
            const state = !ep.up ? 'down' : h.working ? 'busy' : 'up';
            const bits = [ep.up ? (h.kind === 'api' ? `Endpoint up${ep.ms != null ? ` (${ep.ms} ms)` : ''}` : 'Ready')
                                : `Endpoint down${ep.error ? `: ${ep.error}` : ''}`];
            if (h.working) bits.push('working');
            if (h.queued) bits.push(`${h.queued} queued`);
            if (h.last_ok) bits.push(`last reply ${ago(h.last_ok)}`);
            node.append(el('span', { class: `agents-health-dot ${state}`, 'aria-hidden': 'true' }), bits.join(' · '));
            if (h.last_error) {
                node.append(el('div', { class: 'agents-health-error',
                    text: `Last error ${ago(h.last_error.at)} in #${h.last_error.channel}: ${h.last_error.why}` }));
            }
        });
    }

    // Less-used actions live in a "More" menu so each row stays on one line
    function moreMenu(a) {
        const menu = el('details', { class: 'agents-more' });
        const dots = el('summary', { class: 'agents-btn agents-more-btn', title: 'More actions', 'aria-label': `More actions for ${a.name}` });
        dots.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><circle cx="3" cy="8" r="1.5" fill="currentColor"/><circle cx="8" cy="8" r="1.5" fill="currentColor"/><circle cx="13" cy="8" r="1.5" fill="currentColor"/></svg>';
        const item = (text, onclick, cls) => el('button', { class: 'agents-more-item' + (cls ? ' ' + cls : ''), text,
            onclick: (e) => { menu.open = false; onclick(e); } });
        menu.append(dots, el('div', { class: 'agents-more-list', role: 'menu' },
            item('New context', async () => {
                if (!confirm(`Give @${a.name} a fresh context? Its session memory is dropped; its persona stays.`)) return;
                agents = await api('POST', `/api/managed-agents/${encodeURIComponent(a.name)}/fresh`);
                renderList();
            }),
            item('Duplicate', () => renderForm({ ...a, name: '', label: '', persona: '', duplicateOf: a.name })),
            item('Delete', async () => {
                if (!confirm(`Delete ${a.name}? Its settings and session memory are removed; chat history stays.`)) return;
                agents = await api('DELETE', `/api/managed-agents/${encodeURIComponent(a.name)}`);
                renderList();
            }, 'danger')));
        return menu;
    }

    function providerLabel(a) {
        const p = Object.values(PROVIDERS).find(p => p.base_url && p.base_url === a.base_url);
        return p ? p.label : a.base_url;
    }

    async function save(spec) {
        try {
            agents = await api('POST', '/api/managed-agents', spec);
            renderList();
            return true;
        } catch (e) {
            alert(e.message);
            agents = await api('GET', '/api/managed-agents');
            renderList();
            return false;
        }
    }

    function field(label, input, hint) {
        return el('label', { class: 'agents-field' }, el('span', { text: label }), input,
            hint ? el('small', { text: hint }) : null);
    }

    function renderForm(a) {
        const d = dialog();
        d.textContent = '';
        const editing = !!a.name;
        const allChannels = ((typeof channelList !== 'undefined' && channelList.length) ? channelList : []).filter(c => c !== 'general');
        const chosen = new Set(a.channels || []);
        const chanBoxes = allChannels.map(c => {
            const cb = el('input', { type: 'checkbox', value: c, checked: chosen.has(c) });
            return { c, cb, label: el('label', { class: 'agents-toggle' }, cb, el('span', { text: '#' + c })) };
        });
        const f = el('form', { class: 'agents-form' });
        const name = el('input', { required: true, pattern: '[a-z0-9][a-z0-9_-]{0,31}', value: a.name || '',
            placeholder: 'e.g. claude, agy, qwen', readonly: editing });
        const label = el('input', { value: a.label || '', placeholder: 'Display name (optional)' });
        const color = el('input', { type: 'color', value: a.color || '#8b5cf6' });
        const sound = el('select', { 'aria-label': 'Notification sound' });
        const AS = window.AgentSound;
        sound.append(el('option', { value: '', text: 'Room default' }));
        (AS ? AS.options : []).forEach(o => sound.append(el('option', { value: o.value, text: o.label })));
        sound.value = AS && a.name ? AS.get(a.name) : '';
        sound.addEventListener('change', () => AS && AS.preview(sound.value));
        const kind = el('select');
        for (const [v, t] of Object.entries(KINDS)) kind.append(el('option', { value: v, text: t }));
        kind.value = a.kind || 'api';
        const prompt = el('textarea', { rows: 4, placeholder: 'Role and instructions, e.g. "You are the architect. Review designs for gaps before anyone builds."' });
        prompt.value = a.system_prompt || '';
        const persona = el('textarea', { rows: 4, placeholder: 'Empty: the agent writes its own the first time it is asked (e.g. "@all pick a persona for yourselves").' });
        persona.value = a.persona || '';

        // API fields
        const provider = el('select');
        for (const [v, p] of Object.entries(PROVIDERS)) provider.append(el('option', { value: v, text: p.label }));
        provider.value = Object.keys(PROVIDERS).find(k => PROVIDERS[k].base_url && PROVIDERS[k].base_url === a.base_url) || (a.base_url ? 'custom' : 'strata');
        const baseUrl = el('input', { value: a.base_url || PROVIDERS[provider.value].base_url, placeholder: 'https://.../v1' });
        const apiFormat = el('select');
        apiFormat.append(el('option', { value: 'openai', text: 'OpenAI-compatible' }), el('option', { value: 'anthropic', text: 'Anthropic' }));
        apiFormat.value = a.api_format || PROVIDERS[provider.value].api_format;
        const providerHelp = el('small');
        const showHelp = () => { providerHelp.textContent = PROVIDERS[provider.value].help || ''; };
        provider.addEventListener('change', () => {
            const p = PROVIDERS[provider.value];
            if (p.base_url) baseUrl.value = p.base_url;
            apiFormat.value = p.api_format;
            showHelp();
            if (p.base_url) loadModels(true);
        });
        showHelp();
        const apiKey = el('input', { type: 'password', autocomplete: 'off',
            placeholder: a.has_api_key ? (a.duplicateOf ? `Blank = use @${a.duplicateOf}'s key` : 'Stored. Leave blank to keep it.') : 'API key (blank for local servers)' });
        const model = el('input', { list: 'agents-models', value: a.model || '', placeholder: 'Model id' });
        const models = el('datalist', { id: 'agents-models' });
        const fetchBtn = el('button', { type: 'button', class: 'agents-btn', text: 'Fetch models' });
        // quiet: automatic load (provider picked or form opened), no alert or focus
        async function loadModels(quiet) {
            fetchBtn.textContent = 'Fetching…';
            try {
                const ids = await api('POST', '/api/managed-agents/models',
                    { base_url: baseUrl.value, api_key: apiKey.value, api_format: apiFormat.value, name: name.value });
                models.textContent = '';
                ids.forEach(id => models.append(el('option', { value: id })));
                fetchBtn.textContent = `${ids.length} models`;
                if (!quiet) model.focus();
            } catch (e) {
                fetchBtn.textContent = 'Fetch models';
                if (!quiet) alert(e.message);
            }
        }
        fetchBtn.addEventListener('click', () => loadModels(false));
        if (baseUrl.value && provider.value === 'openrouter') loadModels(true);  // public list, no key needed
        const temp = el('input', { type: 'number', step: '0.1', min: '0', max: '2', value: a.temperature ?? '', placeholder: 'default' });
        const ctx = el('input', { type: 'number', min: '1', max: '500', value: a.context_messages || 30 });

        // CLI fields
        const cwd = el('input', { value: a.cwd || '', placeholder: '/data/projects/myapp (default: home)' });
        const cliModel = el('input', { value: a.model || '', placeholder: 'default model' });
        const perm = el('select');
        for (const [v, t] of Object.entries(PERMS)) perm.append(el('option', { value: v, text: t }));
        perm.value = a.permissions || 'read';
        const allowedCmds = el('input', { value: (a.allowed_commands || []).join(', '),
            placeholder: 'e.g. slop-chop, git (blank: none)' });
        const timeout = el('input', { type: 'number', min: '10', max: '7200', value: a.timeout || '', placeholder: '600' });
        const steps = el('input', { type: 'number', min: '1', max: '200', value: a.max_tool_steps || '', placeholder: '40' });
        const requireTool = el('input', { type: 'checkbox' });
        requireTool.checked = !!a.require_tool;
        const effort = el('select', { 'aria-label': 'Thinking effort' });
        for (const [v, t] of [['', 'Channel setting (Think selector)'], ['none', 'Off'], ['low', 'Low'], ['medium', 'Medium'],
                              ['high', 'High'], ['xhigh', 'Extra high'], ['max', 'Max']]) effort.append(el('option', { value: v, text: t }));
        effort.value = a.effort || '';
        const compact = el('input', { type: 'checkbox' });
        compact.checked = !a.no_compact;
        // Voice (read-aloud and conversations): provider + voice from Settings > Voice
        const vProvider = el('select', { 'aria-label': 'Voice provider' });
        const vVoice = el('select', { 'aria-label': 'Voice' });
        const vSpeed = el('input', { type: 'number', min: '0.5', max: '2', step: '0.05', value: a.voice_speed || '', placeholder: '1.0' });
        const vPreview = el('button', { type: 'button', class: 'agents-btn', text: 'Preview' });
        let voiceConf = null;
        const fillVoices = () => {
            const list = (voiceConf?.voices?.[vProvider.value] || {}).voices || [];
            vVoice.textContent = '';
            vVoice.append(el('option', { value: '', text: 'Provider default' }), ...list.map(v => el('option', { value: v, text: v })));
            vVoice.value = vProvider.value === (a.voice_provider || '') && list.includes(a.voice) ? a.voice : '';
        };
        api('GET', '/api/voice/config').then(conf => {
            voiceConf = conf;
            vProvider.append(el('option', { value: '', text: `Default (${conf.default_provider})` }),
                ...conf.providers.map(p => el('option', { value: p.name, text: p.name })));
            vProvider.value = a.voice_provider || '';
            fillVoices();
        }).catch(() => vProvider.append(el('option', { value: a.voice_provider || '', text: a.voice_provider || 'Voice service unavailable' })));
        vProvider.addEventListener('change', fillVoices);
        vPreview.addEventListener('click', async () => {
            vPreview.disabled = true;
            try {
                const res = await fetch('/api/voice/preview', { method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
                    body: JSON.stringify({ provider: vProvider.value, voice: vVoice.value, speed: Number(vSpeed.value) || null,
                                           text: `Hi, I'm ${name.value.trim() || 'your agent'}. This is how I sound.` }) });
                if (!res.ok) throw new Error((await res.json()).error || 'preview failed');
                const url = URL.createObjectURL(await res.blob());
                const au = new Audio(url);
                au.onended = () => URL.revokeObjectURL(url);
                await au.play();
            } catch (err) { alert(err.message); } finally { vPreview.disabled = false; }
        });
        const permWarn = el('small', { class: 'agents-warn',
            text: 'Full access: the agent runs any command and edits any file on this machine without asking. Anyone who can post in the room can direct it.' });

        const apiBox = el('div', { class: 'agents-group' },
            (() => { const f = field('Provider', provider); f.append(providerHelp); return f; })(), field('Base URL', baseUrl), field('API format', apiFormat),
            field('API key', apiKey, 'Stored on the server only, never shown again.'),
            el('div', { class: 'agents-inline' }, field('Model', model), fetchBtn), models,
            el('div', { class: 'agents-inline' }, field('Temperature', temp), field('Context messages', ctx),
                field('Timeout (seconds)', timeout), field('Max tool steps per turn', steps)),
            field('Must use a tool first', requireTool, 'Every turn starts with a tool call (e.g. read_file), so the agent can\'t answer from memory.'),
            field('Remember older messages', compact, 'Messages that leave the context window are summarised into the agent\'s memory for that channel. /compact @name forces it; /forget @name clears it.'));
        const cliBox = el('div', { class: 'agents-group' }, field('Model', cliModel));
        const toolsBox = el('div', { class: 'agents-group' },
            field('Working directory', cwd, 'Where the agent reads, edits and runs things.'),
            field('Permissions', perm, 'Switch any time from the agent list; applies from the next turn.'), permWarn,
            field('Allowed commands', allowedCmds,
                'Programs this agent may run even below Full access (comma-separated). Needed for skills that wrap a CLI, like slop-chop. Not supported for Antigravity.'));
        const sync = () => {
            apiBox.hidden = kind.value !== 'api';
            cliBox.hidden = kind.value === 'api';
            permWarn.hidden = perm.value !== 'full';
        };
        kind.addEventListener('change', sync);
        perm.addEventListener('change', sync);
        sync();

        f.append(
            el('h2', { text: editing ? `Edit @${a.name}` : (a.duplicateOf ? `New instance of @${a.duplicateOf}` : 'Add agent') }),
            el('div', { class: 'agents-inline' }, field('Name (@mention)', name), field('Label', label)),
            el('div', { class: 'agents-inline agents-inline-top' }, field('Color', color),
                field('Notification sound', sound, 'Plays for this agent\'s messages, in this browser.')),
            field('Type', kind),
            apiBox, cliBox,
            field('Thinking effort', effort, 'Overrides the Think selector for this agent. Claude Code uses every level except Off; API agents treat Extra high and Max as High; Antigravity ignores it.'),
            toolsBox,
            field('Role / instructions (from you)', prompt),
            el('div', { class: 'agents-field' }, el('span', { text: 'Voice (read aloud and conversations)' }),
                el('div', { class: 'agents-inline' }, field('Provider', vProvider), field('Voice', vVoice), field('Speed', vSpeed), vPreview),
                el('small', { text: 'Providers and cloned voices are managed in Settings › Voice.' })),
            field('Persona (the agent\'s own identity; it can rewrite this itself)', persona,
                'Kept across restarts and New context. Duplicates and spawns start without one.'),
            el('div', { class: 'agents-field' },
                el('span', { text: 'Channels besides #general (every agent is always in #general)' }),
                el('div', { class: 'agents-inline' }, ...chanBoxes.map(x => x.label)),
                el('small', { text: allChannels.length ? 'Other channels only contain the agents added to them. Coding agents keep a separate session per channel.' : 'No other channels yet. Create one with + in the channel list.' })),
            el('div', { class: 'agents-actions' },
                el('button', { type: 'button', class: 'agents-btn', text: 'Cancel', onclick: renderList }),
                el('button', { type: 'submit', class: 'agents-btn primary', text: editing ? 'Save' : 'Add and start' })));
        f.addEventListener('submit', async (e) => {
            e.preventDefault();
            if (perm.value === 'full' && a.permissions !== 'full' &&
                !confirm('Full access lets this agent run any command on this machine without asking. Continue?')) return;
            const spec = {
                name: name.value.trim(), label: label.value.trim(), color: color.value, kind: kind.value,
                system_prompt: prompt.value.trim(), persona: persona.value.trim(), enabled: a.enabled !== false,
                channels: chanBoxes.filter(x => x.cb.checked).map(x => x.c),
                cwd: cwd.value.trim(), permissions: perm.value,
                allowed_commands: allowedCmds.value.split(',').map(x => x.trim()).filter(Boolean),
                voice_provider: vProvider.value, voice: vVoice.value, voice_speed: Number(vSpeed.value) || null,
                effort: effort.value || null,
            };
            if (a.duplicateOf) spec.copy_key_from = a.duplicateOf;  // reuse the stored API key
            if (kind.value === 'api') {
                Object.assign(spec, { base_url: baseUrl.value.trim(), api_format: apiFormat.value,
                    api_key: apiKey.value, model: model.value.trim(),
                    temperature: temp.value === '' ? null : Number(temp.value),
                    context_messages: Number(ctx.value) || 30, timeout: Number(timeout.value) || null,
                    max_tool_steps: Number(steps.value) || null, require_tool: requireTool.checked,
                    no_compact: !compact.checked });
            } else {
                Object.assign(spec, { model: cliModel.value.trim() });
            }
            if (await save(spec) && window.AgentSound) window.AgentSound.set(spec.name, sound.value);
        });
        d.append(f);
        (editing ? prompt : name).focus();
    }

    document.addEventListener('click', (e) => {
        document.querySelectorAll('.agents-more[open]').forEach(m => { if (!m.contains(e.target)) m.open = false; });
    });

    window.AgentsSection = { mount };
    window.SettingsUI = { el, api };
})();
