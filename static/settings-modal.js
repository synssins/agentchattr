// Settings window (gear icon): categories on the left, settings on the right.
// General reuses the original settings bar (moved in, handlers intact); the other
// sections talk to admin_api.py / agent_runner.py. Secrets are shown once.

(function () {
    const { el, api } = window.SettingsUI;

    const SECTIONS = [
        { id: 'general', label: 'General', render: renderGeneral },
        { id: 'agents', label: 'Agents', render: (p) => window.AgentsSection.mount(p) },
        { id: 'connections', label: 'Connections', render: renderConnections },
        { id: 'apikeys', label: 'API keys', render: renderApiKeys },
        { id: 'secrets', label: 'Secrets', render: renderSecrets },
        { id: 'irc', label: 'IRC', render: renderIrc },
    ];
    let current = 'general';

    function dialog() {
        let d = document.getElementById('settings-dialog');
        if (d) return d;
        d = el('dialog', { id: 'settings-dialog', class: 'settings-dialog', 'aria-label': 'Settings' });
        const nav = el('nav', { class: 'settings-nav', 'aria-label': 'Settings categories' },
            el('div', { class: 'settings-nav-title', text: 'Settings' }));
        for (const s of SECTIONS) {
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
        SECTIONS.find(s => s.id === id).render(panel);
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

    // ------------------------------------------------------------ General
    function renderGeneral(panel) {
        heading(panel, 'General', 'Your name in the room, appearance and loop guard. Changes save automatically.');
        const bar = document.getElementById('settings-bar');
        if (bar) {
            bar.classList.remove('hidden');
            panel.append(bar);
        }
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
