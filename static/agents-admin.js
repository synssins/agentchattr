// Agents manager: create, edit, enable/disable and delete room agents that the
// server runs itself (agent_runner.py). Everything is built with createElement /
// textContent. API keys are write-only: the server never sends them back.
// Layout parts come from UIKit (ui.js); styles are in agents.css (.ag-*).

(function () {
    const PROVIDERS = {
        strata:     { label: 'Nexus router (local)',  base_url: 'http://127.0.0.1:4000/v1', api_format: 'openai' },
        openrouter: { label: 'OpenRouter',      base_url: 'https://openrouter.ai/api/v1', api_format: 'openai',
                      help: 'Paste a key from openrouter.ai/keys, then pick a model from the list (type to filter, e.g. "claude" or "free").' },
        gemini:     { label: 'Google Gemini',   base_url: 'https://generativelanguage.googleapis.com/v1beta/openai', api_format: 'openai' },
        anthropic:  { label: 'Anthropic',       base_url: 'https://api.anthropic.com/v1', api_format: 'anthropic' },
        custom:     { label: 'Custom (OpenAI-compatible)', base_url: '', api_format: 'openai' },
    };
    const KINDS = { api: 'LLM API (OpenRouter, Strata, Gemini, Anthropic, OpenAI-compatible)', claude: 'Claude Code (CLI)', agy: 'Antigravity (CLI)' };
    const KIND_SHORT = { api: 'LLM API', claude: 'Claude Code', agy: 'Antigravity' };
    const PERMS = {
        read: 'Read-only',
        edit: 'Can edit files',
        full: 'Full access (no approvals)',
    };
    const PERMS_SHORT = { read: 'Read-only', edit: 'Edit files', full: 'Full access' };

    let agents = [];
    let loadError = '';
    let notice = null;                                   // {text, error}: shown once above the list
    const view = { q: '', state: 'all', kind: 'all' };   // list filter, kept across re-renders

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

    const SVG_NS = 'http://www.w3.org/2000/svg';
    function icon(d) {
        const s = document.createElementNS(SVG_NS, 'svg');
        const attrs = { width: 14, height: 14, viewBox: '0 0 16 16', fill: 'none', stroke: 'currentColor',
                        'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' };
        for (const [k, v] of Object.entries(attrs)) s.setAttribute(k, v);
        const p = document.createElementNS(SVG_NS, 'path');
        p.setAttribute('d', d);
        s.append(p);
        return s;
    }
    const ICON_MORE = 'M3 8h.01M8 8h.01M13 8h.01';
    const ICON_BACK = 'M10 3 5 8l5 5';

    let root = null;   // the settings panel this section renders into
    let page = null;   // our own node inside it: late async results land here, never in another section

    async function reload() {
        try { agents = await api('GET', '/api/managed-agents'); loadError = ''; }
        catch (e) { agents = []; loadError = e.message; }
    }

    async function mount(container) {
        root = container;
        const mine = page = el('div', { class: 'ag-page' }, el('p', { class: 'ag-loading', role: 'status', text: 'Loading…' }));
        root.textContent = '';
        root.append(page);
        await reload();
        if (page === mine) renderList();
    }

    const fullAccessPrompt = (name) => `Give ${name} full access?\n\nIt will run any command and edit any file on this machine ` +
        `without asking, and anyone who can post in this room can tell it what to do.`;

    function permSelect(a) {
        const sel = el('select', { class: 'ag-perm', 'aria-label': `Permissions for ${a.name}`, 'data-k': `perm:${a.name}` });
        for (const [v, label] of Object.entries(PERMS_SHORT)) sel.append(el('option', { value: v, text: label, title: PERMS[v] }));
        sel.value = a.permissions || 'read';
        sel.dataset.perm = sel.value;
        sel.title = PERMS[sel.value] || '';
        sel.addEventListener('change', async () => {
            if (sel.value === 'full' && !confirm(fullAccessPrompt(a.name))) {
                sel.value = a.permissions || 'read';
                return;
            }
            await save({ ...a, permissions: sel.value });
        });
        return sel;
    }

    // What runs the agent, in one line
    function runsLine(a) {
        return (a.kind === 'api' ? [KIND_SHORT.api, a.model || '?', providerLabel(a)]
                                 : [KIND_SHORT[a.kind] || a.kind, a.model || 'default model', a.cwd || '~']).filter(Boolean).join(' · ');
    }

    // Badge text and tone. h: this agent's health entry; loaded: the health list has arrived.
    function stateOf(a, h, loaded) {
        if (a.needs_llm) return ['Needs LLM server', 'warn'];
        if (h) return !(h.endpoint || {}).up ? ['Endpoint down', 'danger'] : h.working ? ['Working', 'accent'] : ['Running', 'ok'];
        if (a.enabled === false) return ['Off', ''];
        return !loaded && a.running ? ['Running', 'ok'] : ['Not running', 'warn'];
    }

    function agentRow(a) {
        const on = el('input', { type: 'checkbox', class: 'ag-switch', role: 'switch', checked: a.enabled !== false,
            'aria-label': `Enable ${a.name}`, 'data-k': `on:${a.name}`,
            disabled: !!a.needs_llm, title: a.needs_llm ? 'Edit it and pick an LLM server first' : '' });
        on.addEventListener('change', () => save({ ...a, enabled: on.checked }));
        const dot = el('span', { class: 'ag-dot', 'aria-hidden': 'true' });
        dot.style.background = a.color || '#8b5cf6';
        const [stateText, tone] = stateOf(a, null, false);
        const state = el('span', { class: 'ag-state', 'data-agent': a.name }, window.UIKit.badge(stateText, tone));
        const warn = a.needs_llm ? 'Offline: needs an LLM server. Edit it and pick one.' : a.llm_error;
        const node = el('div', { class: 'ag-row', role: 'listitem' },
            dot,
            el('div', { class: 'ag-info' },
                el('div', { class: 'ag-main' },
                    el('div', { class: 'ag-title' },
                        a.label ? el('span', { class: 'ag-label', text: a.label }) : null,
                        el('span', { class: a.label ? 'ag-handle' : 'ag-label', text: `@${a.name}` }),
                        state,
                        a.spawned ? window.UIKit.badge('Spawned') : null),
                    el('div', { class: 'ag-runs', text: runsLine(a), title: runsLine(a) })),
                el('div', { class: 'ag-channels', 'aria-label': 'Channels' },
                    ...['general', ...(a.channels || [])].map(c => el('span', { text: '#' + c })))),
            el('div', { class: 'ag-controls' },
                permSelect(a),
                el('label', { class: 'ag-toggle' }, on, el('span', { text: 'On' })),
                el('button', { type: 'button', class: 'ui-btn sm', text: 'Edit', 'aria-label': `Edit ${a.name}`,
                    'data-k': `edit:${a.name}`, onclick: () => renderForm(a) }),
                moreMenu(a)),
            warn ? el('div', { class: 'ag-rowmsg warn', text: warn }) : null,
            el('div', { class: 'ag-rowmsg error', 'data-error': a.name, hidden: true }));
        const hay = [a.name, a.label, a.model, a.llm_model, KIND_SHORT[a.kind], a.kind === 'api' ? providerLabel(a) : a.cwd]
            .filter(Boolean).join(' ').toLowerCase();
        return { a, node, hay };
    }

    function renderList() {
        const active = document.activeElement;
        const keep = active && page.contains(active) ? active.dataset.k : null;
        const top = root.scrollTop;
        page.textContent = '';
        const count = el('span', { class: 'ag-count' });
        page.append(el('div', { class: 'ag-head' },
            el('h2', { text: 'Room agents' }), count,
            el('button', { type: 'button', class: 'ui-btn primary', text: 'Add agent', 'data-k': 'add', onclick: () => renderForm({}) })));
        page.append(el('div', { class: 'ag-notice' + (notice && notice.error ? ' error' : ''),
            role: notice && notice.error ? 'alert' : 'status', text: notice ? notice.text : '' }));
        notice = null;
        if (loadError) {
            page.append(window.UIKit.empty('Agents could not be loaded',
                `${loadError}. Check that the server is running, then try again.`,
                el('button', { type: 'button', class: 'ui-btn', text: 'Try again', onclick: () => mount(root) })));
            return;
        }
        if (!agents.length) {
            page.append(window.UIKit.empty('No room agents yet',
                'An agent is an LLM (OpenRouter, Gemini, Anthropic, a local server) or a coding agent (Claude Code, Antigravity) that runs here, headless.',
                el('button', { type: 'button', class: 'ui-btn primary', text: 'Add agent', onclick: () => renderForm({}) })));
            return;
        }

        const rows = agents.map(agentRow);
        const list = el('div', { class: 'ag-list', role: 'list' }, ...rows.map(r => r.node));
        const filter = el('input', { type: 'search', class: 'ag-filter', placeholder: 'Filter by name or model',
            'aria-label': 'Filter agents by name or model', 'data-k': 'filter', value: view.q });
        const clear = () => {
            view.q = filter.value = ''; view.state = view.kind = 'all';
            page.querySelectorAll('.ag-chip').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === 'all')));
            apply();
        };
        const none = window.UIKit.empty('No agents match', 'No agent has that name, model, state or type.',
            el('button', { type: 'button', class: 'ui-btn', text: 'Clear filter', onclick: clear }));
        function apply() {
            const q = view.q.trim().toLowerCase();
            let shown = 0;
            for (const r of rows) {
                const hit = (!q || r.hay.includes(q)) && (view.kind === 'all' || r.a.kind === view.kind) &&
                            (view.state === 'all' || (view.state === 'on') === !!r.a.running);
                r.node.hidden = !hit;
                if (hit) shown++;
            }
            list.hidden = !shown;
            none.hidden = !!shown;
            const running = agents.filter(a => a.running).length;
            count.textContent = (shown === agents.length ? `${agents.length}` : `${shown} of ${agents.length}`) + ` · ${running} running`;
        }
        const chips = (label, prop, opts) => {
            const g = el('div', { class: 'ag-chips', role: 'group', 'aria-label': label });
            for (const [v, t] of opts) g.append(el('button', { type: 'button', class: 'ag-chip', 'data-v': v, text: t,
                'aria-pressed': String(view[prop] === v),
                onclick: () => {
                    view[prop] = v;
                    for (const b of g.children) b.setAttribute('aria-pressed', String(b.dataset.v === v));
                    apply();
                } }));
            return g;
        };
        filter.addEventListener('input', () => { view.q = filter.value; apply(); });
        const kinds = Object.keys(KIND_SHORT).filter(k => agents.some(a => a.kind === k));
        if (!kinds.includes(view.kind)) view.kind = 'all';
        page.append(el('div', { class: 'ag-toolbar' }, filter,
            chips('Filter by state', 'state', [['all', 'All'], ['on', 'Running'], ['off', 'Not running']]),
            kinds.length > 1 ? chips('Filter by type', 'kind', [['all', 'All types'], ...kinds.map(k => [k, KIND_SHORT[k]])]) : null));
        page.append(list, none);
        apply();
        root.scrollTop = top;
        const again = keep && [...page.querySelectorAll('[data-k]')].find(n => n.dataset.k === keep);
        if (again) again.focus({ preventScroll: true });
        loadHealth(list);
    }

    function ago(t) {
        const s = Math.max(0, Math.round(Date.now() / 1000 - t));
        return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`;
    }

    // Health per agent: badge (endpoint up/down, working), response time, queued, last reply, last error
    async function loadHealth(list) {
        let rows = [];
        try { rows = await api('GET', '/api/managed-agents/health'); } catch { /* keep the state from the list */ return; }
        const by = Object.fromEntries(rows.map(h => [h.name, h]));
        list.querySelectorAll('.ag-state').forEach(node => {
            const a = agents.find(x => x.name === node.dataset.agent);
            if (!a) return;
            const h = by[a.name];
            const [text, tone] = stateOf(a, h, true);
            node.textContent = '';
            node.append(window.UIKit.badge(text, tone));
            if (!h) return;
            const ep = h.endpoint || {};
            const bits = [];
            if (!ep.up && ep.error) bits.push(ep.error);
            if (ep.up && h.kind === 'api' && ep.ms != null) bits.push(`${ep.ms} ms`);
            if (h.queued) bits.push(`${h.queued} queued`);
            if (h.last_ok) bits.push(`last reply ${ago(h.last_ok)}`);
            if (bits.length) node.append(el('span', { class: 'ag-state-detail', text: bits.join(' · ') }));
            if (h.last_error) {
                const err = [...list.querySelectorAll('[data-error]')].find(n => n.dataset.error === a.name);
                if (err) {
                    err.textContent = `Last error ${ago(h.last_error.at)} in #${h.last_error.channel}: ${h.last_error.why}`;
                    err.hidden = false;
                }
            }
        });
    }

    // Both ask first. -> true when done, false when declined; throw on a server error.
    async function freshContext(a) {
        if (!confirm(`Give @${a.name} a fresh context? Its session memory is dropped; its persona and notes stay.`)) return false;
        agents = await api('POST', `/api/managed-agents/${encodeURIComponent(a.name)}/fresh`);
        return true;
    }
    async function removeAgent(a) {
        if (!confirm(`Delete ${a.name}? Its settings, persona, notes and session memory are removed; chat history stays.`)) return false;
        agents = await api('DELETE', `/api/managed-agents/${encodeURIComponent(a.name)}`);
        return true;
    }

    // Less-used actions live in a "More" menu so each row stays compact
    function moreMenu(a) {
        const menu = el('details', { class: 'ag-more' });
        const dots = el('summary', { class: 'ui-btn sm ag-more-btn', title: 'More actions', 'aria-label': `More actions for ${a.name}`,
            'data-k': `more:${a.name}` }, icon(ICON_MORE));
        const item = (text, onclick, cls) => el('button', { type: 'button', class: 'ag-more-item' + (cls ? ' ' + cls : ''), text,
            onclick: (e) => { menu.open = false; onclick(e); } });
        const run = (action, done) => async () => {
            try { if (await action(a)) { notice = { text: done }; renderList(); } }
            catch (e) { notice = { text: `${e.message}. Nothing was changed for @${a.name}; try again.`, error: true }; renderList(); }
        };
        menu.append(dots, el('div', { class: 'ag-more-list' },
            item('New context', run(freshContext, `@${a.name} has a fresh context.`)),
            item('Duplicate', () => renderForm({ ...a, name: '', label: '', persona: '', duplicateOf: a.name })),
            item('Delete', run(removeAgent, `Deleted @${a.name}.`), 'danger')));
        menu.addEventListener('keydown', (e) => { if (e.key === 'Escape' && menu.open) { e.preventDefault(); e.stopPropagation(); menu.open = false; dots.focus(); } });
        return menu;
    }

    function providerLabel(a) {
        const p = Object.values(PROVIDERS).find(p => p.base_url && p.base_url === a.base_url);
        return p ? p.label : a.base_url;
    }

    // Save from the list (switch, access level): the list is redrawn either way
    async function save(spec) {
        try {
            agents = await api('POST', '/api/managed-agents', spec);
            notice = { text: `Saved @${spec.name}.` };
            renderList();
            return true;
        } catch (e) {
            await reload();
            notice = { text: `Could not save @${spec.name}: ${e.message}. The list shows the stored settings; change it again to retry.`, error: true };
            renderList();
            return false;
        }
    }

    function renderForm(a) {
        const { group, field } = window.UIKit;
        const F = (label, control, hint, stack) => field(label, control, { hint, stack });
        const num = (attrs) => el('input', { type: 'number', class: 'ag-num', ...attrs });
        page.textContent = '';
        const editing = !!a.name;
        const allChannels = ((typeof channelList !== 'undefined' && channelList.length) ? channelList : []).filter(c => c !== 'general');
        const chosen = new Set(a.channels || []);
        const chanBoxes = allChannels.map(c => {
            const cb = el('input', { type: 'checkbox', value: c, checked: chosen.has(c) });
            return { c, cb, label: el('label', { class: 'ag-check' }, cb, el('span', { text: '#' + c })) };
        });
        const f = el('form', { class: 'ag-form', novalidate: true });
        const name = el('input', { required: true, pattern: '[a-z0-9][a-z0-9_-]{0,31}', value: a.name || '',
            placeholder: 'e.g. claude, agy, qwen', readonly: editing });
        const label = el('input', { value: a.label || '', placeholder: 'Display name (optional)' });
        const color = el('input', { type: 'color', value: a.color || '#8b5cf6' });
        const sound = el('select');
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

        // API fields: the agent links to an LLM server (Settings > LLMs) and uses its default
        // model or one the server advertises
        const server = el('select');
        const model = el('select');
        const serverHelp = el('small', { class: 'ag-warn', role: 'status' });
        let servers = [], serversLoaded = false;
        async function loadServerModels() {
            const s = servers.find(x => x.id === server.value);
            const keep = model.value || a.llm_model || '';
            model.textContent = '';
            model.append(el('option', { value: '', text: `Default (${(s && s.default_model) || 'none set'})` }));
            if (keep) model.append(el('option', { value: keep, text: keep }));
            model.value = keep;
            serverHelp.textContent = s ? '' : 'Add a server in Settings > LLMs first.';
            if (!s) return;
            try {
                const r = await api('POST', `/api/admin/llms/${encodeURIComponent(s.id)}/test`);
                model.firstChild.textContent = `Default (${r.default_model || 'none set'})`;
                r.models.filter(m => m !== keep).forEach(m => model.append(el('option', { value: m, text: m })));
            } catch (e) { serverHelp.textContent = `Couldn't list models: ${e.message}`; }
        }
        server.addEventListener('change', () => { model.value = ''; loadServerModels(); });
        server.append(el('option', { value: '', text: 'Loading…' }));
        api('GET', '/api/admin/llms').then(list => {
            servers = list;
            serversLoaded = true;
            server.textContent = '';
            server.append(el('option', { value: '', text: 'Pick a server' }));
            list.filter(x => !x.readonly).forEach(x => server.append(el('option', { value: x.id, text: x.name })));
            server.value = a.llm || '';
            loadServerModels();
            notes();
        }).catch(() => { server.firstChild.textContent = 'Unavailable'; serverHelp.textContent = 'LLM servers unavailable. Reopen this form to retry.'; });
        const temp = num({ step: '0.1', min: '0', max: '2', value: a.temperature ?? '', placeholder: 'default' });
        const ctx = num({ min: '1', max: '500', value: a.context_messages || 30 });

        // CLI fields
        const cwd = el('input', { value: a.cwd || '', placeholder: '/data/projects/myapp (default: home)' });
        const cliModel = el('input', { value: a.model || '', placeholder: 'default model' });
        const perm = el('select');
        for (const [v, t] of Object.entries(PERMS)) perm.append(el('option', { value: v, text: t }));
        perm.value = a.permissions || 'read';
        const allowedCmds = el('input', { value: (a.allowed_commands || []).join(', '),
            placeholder: 'e.g. slop-chop, git (blank: none)' });
        const timeout = num({ min: '10', max: '7200', value: a.timeout || '', placeholder: '600' });
        const steps = num({ min: '1', max: '200', value: a.max_tool_steps || '', placeholder: '40' });
        const requireTool = el('input', { type: 'checkbox', class: 'ag-switch', role: 'switch' });
        requireTool.checked = !!a.require_tool;
        const effort = el('select');
        for (const [v, t] of [['', 'Channel setting (Think selector)'], ['none', 'Off'], ['low', 'Low'], ['medium', 'Medium'],
                              ['high', 'High'], ['xhigh', 'Extra high'], ['max', 'Max']]) effort.append(el('option', { value: v, text: t }));
        effort.value = a.effort || '';
        const compact = el('input', { type: 'checkbox', class: 'ag-switch', role: 'switch' });
        compact.checked = !a.no_compact;
        // Voice (read-aloud and conversations): provider + voice from Settings > Voice
        const vProvider = el('select');
        const vVoice = el('select');
        const vSpeed = num({ min: '0.5', max: '2', step: '0.05', value: a.voice_speed || '', placeholder: '1.0' });
        const vPreview = el('button', { type: 'button', class: 'ui-btn', text: 'Preview' });
        const vPreviewMsg = el('small', { class: 'ag-warn', role: 'alert' });
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
            notes();
        }).catch(() => vProvider.append(el('option', { value: a.voice_provider || '', text: a.voice_provider || 'Voice service unavailable' })));
        vProvider.addEventListener('change', fillVoices);
        vPreview.addEventListener('click', async () => {
            vPreview.disabled = true;
            vPreview.textContent = 'Playing…';
            vPreviewMsg.textContent = '';
            try {
                const res = await fetch('/api/voice/preview', { method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
                    body: JSON.stringify({ provider: vProvider.value, voice: vVoice.value, speed: Number(vSpeed.value) || null,
                                           text: `Hi, I'm ${name.value.trim() || 'your agent'}. This is how I sound.` }) });
                if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || 'preview failed');
                const url = URL.createObjectURL(await res.blob());
                const au = new Audio(url);
                au.onended = () => URL.revokeObjectURL(url);
                await au.play();
            } catch (err) { vPreviewMsg.textContent = `Preview failed: ${err.message}. Check Settings > Voice, then try again.`; }
            finally { vPreview.disabled = false; vPreview.textContent = 'Preview'; }
        });
        // Long-term memory: the agent's own notes (profile folder on the server)
        const memSize = num({ min: '0', max: '16384', step: '256', value: a.memory_size ?? 4096 });
        const memText = el('textarea', { rows: 6, placeholder: editing ? 'No notes yet. The agent adds them itself; notes can also be written here, one per line.' : 'Available after the agent is added.', disabled: !editing });
        const memUsage = el('small', { class: 'ui-field-hint', role: 'status', text: editing ? 'Loading…' : '' });
        const memFolder = el('code', { class: 'ag-path', text: 'Loading…' });
        const clearNotes = el('button', { type: 'button', class: 'ui-btn danger', text: 'Clear notes', disabled: !editing,
            onclick: () => { if (confirm(`Clear all of @${a.name}'s notes? This applies when you save.`)) { memText.value = ''; touch(); } } });
        let memLoaded = null;  // text as loaded: saved only when changed
        if (editing) api('GET', `/api/managed-agents/${encodeURIComponent(a.name)}/memory`).then(m => {
            memText.value = memLoaded = m.text;
            memUsage.textContent = `Using ${m.used} of ${m.size} bytes.`;
            memFolder.textContent = m.folder;
        }).catch((e) => {
            memText.disabled = clearNotes.disabled = true;
            memUsage.textContent = `Notes could not be loaded: ${e.message}. Reopen this form to retry; saving keeps them as they are.`;
            memFolder.textContent = 'Unavailable';
        });
        const permWarn = el('small', { class: 'ag-warn',
            text: 'Full access: the agent runs any command and edits any file on this machine without asking. Anyone who can post in the room can direct it.' });

        const under = (row, ...extra) => { row.querySelector('.ui-field-text').append(...extra); return row; };
        const apiOnly = [
            under(F('LLM server', server, 'Servers, keys and default models are set in Settings > LLMs.'), serverHelp),
            F('Model', model, 'Default follows the server\'s default model; pick one to pin it.'),
            F('Temperature', temp, 'Blank uses the server default.'),
            F('Context messages', ctx, 'Recent messages sent with each turn (1 to 500).'),
            F('Timeout (seconds)', timeout, 'Longest a turn may take; blank uses 600.'),
            F('Max tool steps per turn', steps, 'Blank uses 40.'),
            F('Must use a tool first', requireTool, 'Every turn starts with a tool call (e.g. read_file), so the agent can\'t answer from memory.'),
            F('Remember older messages', compact, 'Summarises messages that leave the context window into channel memory. /compact @name forces it; /forget @name clears it.'),
        ];
        const cliOnly = [F('Model', cliModel, 'Blank uses the CLI\'s default model.')];

        const gIdentity = group('Identity', { id: 'agent-identity', note: '…' },
            F('Name (@mention)', name, editing ? 'The name cannot be changed after the agent is added.'
                                               : 'Lowercase letters, digits, - and _; up to 32 characters.'),
            F('Label', label, 'Display name shown instead of the @name.'),
            F('Color', color, 'Marks the agent in the room and in this list.'),
            F('Type', kind, 'What runs the agent: an LLM API, or a coding CLI on this machine.'));
        const gModel = group('Model', { id: 'agent-model', note: '…' },
            ...apiOnly, ...cliOnly,
            F('Thinking effort', effort, 'Overrides the Think selector. Claude Code uses every level except Off; API agents treat Extra high and Max as High; Antigravity ignores it.'));
        const gPersona = group('Instructions, persona, memory', { id: 'agent-persona', note: '…' },
            F('Role and instructions', prompt, 'Written by the room owner; the agent follows it on every turn.', true),
            F('Persona', persona, 'The agent\'s own identity, which it can rewrite itself. Kept across restarts and New context; duplicates and spawns start without one.', true),
            under(F('Long-term memory', memText, 'The agent\'s own notes about people, projects and decisions, one per line. They follow it across channels, restarts and New context.', true), memUsage),
            F('Size limit (bytes, 0 = off)', memSize, 'The oldest notes are dropped when the limit is reached.'),
            editing ? F('Profile folder', memFolder, 'Where the notes and persona are stored on the server.') : null,
            F('Clear notes', clearNotes, 'Empties the notes above; applies on save.'));
        const gTools = group('Tools and permissions', { id: 'agent-tools', note: '…' },
            under(F('Access level', perm, 'Switch any time from the agent list; applies from the next turn.'), permWarn),
            F('Working directory', cwd, 'Where the agent reads, edits and runs things.'),
            F('Allowed commands', allowedCmds,
                'Comma-separated programs allowed even below Full access, for skills that wrap a CLI like slop-chop. Not supported for Antigravity.'));
        const tagGuide = el('textarea', { rows: 3, maxlength: '800', placeholder: 'e.g. [chuckle] for teasing; [sigh] when something fails; never [angry]' });
        tagGuide.value = a.tag_guide || '';
        const gVoice = group('Voice and sound', { id: 'agent-voice', open: false, note: '…' },
            F('Voice provider', vProvider, 'Used for read aloud and conversations. Providers and cloned voices are managed in Settings > Voice.'),
            F('Voice', vVoice),
            F('Speed', vSpeed, 'From 0.5 to 2; blank is 1.0.'),
            F('Emotion guidance', tagGuide, 'When this agent uses which emotion tag, for voices that perform tags. Blank: the standard guidance.', true),
            under(F('Preview', vPreview, 'Plays a short sample with the provider, voice and speed above.'), vPreviewMsg),
            F('Notification sound', sound, 'Plays for this agent\'s messages, in this browser.'));
        const gChannels = group('Channels', { id: 'agent-channels', open: false, note: '…' },
            F('Channels besides #general', allChannels.length ? el('div', { class: 'ag-checks' }, ...chanBoxes.map(x => x.label)) : null,
                allChannels.length ? 'Every agent is always in #general. Other channels only contain the agents added to them; coding agents keep a separate session per channel.'
                                   : 'Every agent is always in #general. No other channels yet: create one with + in the channel list.', true));

        // Words in the bar: unsaved / saving / saved / error
        const status = el('span', { class: 'ag-status', role: 'status', text: editing ? 'No changes' : 'Not added yet' });
        const setStatus = (text, tone) => { status.textContent = text; status.dataset.tone = tone || ''; };
        const submit = el('button', { type: 'submit', class: 'ui-btn primary', text: editing ? 'Save' : 'Add and start' });
        let dirty = false;
        function touch() { dirty = true; setStatus('Unsaved changes', 'warn'); notes(); }
        const leave = () => { if (!dirty || confirm('Discard the unsaved changes?')) renderList(); };

        const gDanger = editing ? group('Danger zone', { id: 'agent-danger', open: false },
            F('New context', el('button', { type: 'button', class: 'ui-btn danger', text: 'New context', onclick: async () => {
                try { if (await freshContext(a)) setStatus(`@${a.name} has a fresh context.` + (dirty ? ' Unsaved changes.' : ''), dirty ? 'warn' : 'ok'); }
                catch (e) { setStatus(`New context failed: ${e.message}. Try again.`, 'error'); }
            } }), 'Drops the session memory; persona and notes stay.'),
            F('Delete agent', el('button', { type: 'button', class: 'ui-btn danger', text: 'Delete', onclick: async () => {
                try { if (await removeAgent(a)) { notice = { text: `Deleted @${a.name}.` }; renderList(); } }
                catch (e) { setStatus(`Delete failed: ${e.message}. Try again.`, 'error'); }
            } }), 'Removes settings, persona, notes and session memory; chat history stays.')) : null;

        const setNote = (g, text) => { const n = g.querySelector('.ui-group-note'); if (n) n.textContent = text; };
        const picked = (sel) => (sel.selectedOptions[0] || {}).textContent || '';
        function notes() {
            const isApi = kind.value === 'api';
            setNote(gIdentity, [name.value.trim() ? '@' + name.value.trim() : 'New agent', KIND_SHORT[kind.value]].join(' · '));
            setNote(gModel, isApi ? (serversLoaded ? [server.value ? picked(server) : 'No server', model.value || 'default model'].join(' · ') : 'Loading…')
                                  : (cliModel.value.trim() || 'Default model'));
            setNote(gPersona, [prompt.value.trim() ? 'Instructions set' : 'No instructions', persona.value.trim() ? 'persona set' : 'no persona'].join(' · '));
            setNote(gTools, [PERMS_SHORT[perm.value], cwd.value.trim() || 'home folder'].join(' · '));
            setNote(gVoice, [vProvider.value || 'Default provider', vVoice.value || 'default voice'].join(' · '));
            const n = chanBoxes.filter(x => x.cb.checked).length;
            setNote(gChannels, n ? `#general + ${n}` : '#general only');
        }
        const sync = () => {
            apiOnly.forEach(r => { r.hidden = kind.value !== 'api'; });
            cliOnly.forEach(r => { r.hidden = kind.value === 'api'; });
            permWarn.hidden = perm.value !== 'full';
        };
        kind.addEventListener('change', sync);
        perm.addEventListener('change', sync);
        sync();
        notes();

        const title = el('h2', { tabindex: '-1', text: editing ? `Edit @${a.name}` : (a.duplicateOf ? `New instance of @${a.duplicateOf}` : 'Add agent') });
        f.append(
            el('div', { class: 'ag-head' },
                el('button', { type: 'button', class: 'ui-btn ghost sm ag-back', onclick: leave }, icon(ICON_BACK), el('span', { text: 'Agents' })),
                title),
            ...[gIdentity, gModel, gPersona, gTools, gVoice, gChannels, gDanger].filter(Boolean),
            el('div', { class: 'ag-bar' }, status,
                el('button', { type: 'button', class: 'ui-btn', text: 'Cancel', onclick: leave }),
                submit));
        f.addEventListener('input', touch);
        f.addEventListener('change', touch);
        f.addEventListener('submit', async (e) => {
            e.preventDefault();
            // novalidate + this check: a field inside a collapsed group cannot show the browser's own message
            const bad = [...f.elements].find(c => c.willValidate && !c.closest('[hidden]') && !c.checkValidity());
            if (bad) {
                const g = bad.closest('details');
                if (g) g.open = true;
                const row = bad.closest('.ui-field');
                setStatus(`Check "${row ? row.querySelector('.ui-field-label').textContent : 'the marked field'}", then save again.`, 'error');
                bad.reportValidity();
                return;
            }
            if (perm.value === 'full' && a.permissions !== 'full' &&
                !confirm('Full access lets this agent run any command on this machine without asking. Continue?')) return;
            const spec = {
                name: name.value.trim(), label: label.value.trim(), color: color.value, kind: kind.value,
                system_prompt: prompt.value.trim(), persona: persona.value.trim(), enabled: a.enabled !== false,
                channels: chanBoxes.filter(x => x.cb.checked).map(x => x.c),
                cwd: cwd.value.trim(), permissions: perm.value,
                allowed_commands: allowedCmds.value.split(',').map(x => x.trim()).filter(Boolean),
                voice_provider: vProvider.value, voice: vVoice.value, voice_speed: Number(vSpeed.value) || null, tag_guide: tagGuide.value.trim(),
                effort: effort.value || null,
                memory_size: memSize.value === '' ? null : Number(memSize.value),
            };
            if (kind.value === 'api') {
                // until the server list has loaded, keep the stored link rather than sending a blank one
                Object.assign(spec, { llm: serversLoaded ? server.value : (a.llm || ''), llm_model: serversLoaded ? model.value : (a.llm_model || ''),
                    temperature: temp.value === '' ? null : Number(temp.value),
                    context_messages: Number(ctx.value) || 30, timeout: Number(timeout.value) || null,
                    max_tool_steps: Number(steps.value) || null, require_tool: requireTool.checked,
                    no_compact: !compact.checked });
            } else {
                Object.assign(spec, { model: cliModel.value.trim() });
            }
            submit.disabled = true;
            setStatus('Saving…');
            try {
                if (editing && memLoaded !== null && memText.value !== memLoaded) {
                    await api('PUT', `/api/managed-agents/${encodeURIComponent(a.name)}/memory`, { text: memText.value });
                    memLoaded = memText.value;
                }
                agents = await api('POST', '/api/managed-agents', spec);
            } catch (err) {
                // stay here with everything typed; nothing is lost
                submit.disabled = false;
                setStatus(`Not saved: ${err.message}. Fix it and save again.`, 'error');
                return;
            }
            if (window.AgentSound) window.AgentSound.set(spec.name, sound.value);
            notice = { text: editing ? `Saved @${spec.name}.` : `Added @${spec.name}.` };
            renderList();
        });
        page.append(f);
        root.scrollTop = 0;
        (editing ? title : name).focus({ preventScroll: true });
    }

    document.addEventListener('click', (e) => {
        document.querySelectorAll('.ag-more[open]').forEach(m => { if (!m.contains(e.target)) m.open = false; });
    });

    window.AgentsSection = { mount, PROVIDERS };
    window.SettingsUI = { el, api };
})();
