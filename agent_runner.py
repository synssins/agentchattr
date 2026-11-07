"""Room agents managed from the web UI and run inside the server.

Two kinds:
  api    - an LLM endpoint (OpenAI-compatible or Anthropic). Each turn sends
           the last `context_messages` of the channel.
  claude / agy - a coding-agent CLI run headless (`-p`) per turn, resuming its
           own session, so it keeps its own memory and compacts its own
           context; each turn sends only the messages it hasn't seen yet.

Permissions for CLI agents (`permissions`, switchable any time, applies next turn):
  read  - read/search files only
  edit  - may also edit files
  full  - no restrictions (--dangerously-skip-permissions): anyone who can post
          in the room can make this agent run commands on this machine.

Definitions live in data/managed_agents.json (0600: it holds provider API keys;
they are never sent back to the browser).
"""

from __future__ import annotations  # AgentManager.list shadows list[...] in annotations

import json
import logging
import re
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

KINDS = ("api", "claude", "agy")
PERMISSIONS = ("read", "edit", "full")
NO_REPLY = "(no reply)"
CLI_TIMEOUT = 30 * 60

PERSONA_RE = re.compile(r"\[persona\](.*?)\[/persona\]", re.S | re.I)
RENAME_RE = re.compile(r"\[rename\](.*?)\[/rename\]", re.S | re.I)
PERSONA_HELP = (
    "Your persona is your own lasting identity: personality, voice, quirks, how you work with "
    "others. It follows you across projects and context resets. To set or rewrite it, put the "
    "full new text in your reply between [persona] and [/persona] (under 1200 characters). "
    "That block is saved and removed from the posted message. To change your @handle / screen "
    "name (like a Discord or IRC nick, e.g. ScroogeMcDuck19), add [rename]NewName[/rename]: "
    "letters, digits, - or _, max 32, not already taken. It is shown as you type it; "
    "@mentions ignore case.")

ROOM_PROMPT = (
    "You are {name} in AgentHub, a shared chat room with other AI agents and one human "
    "(the owner). Messages addressed to you arrive as a transcript. Reply with only the text "
    "to post in the room: no name prefix, concise unless asked for detail. Mention another "
    "agent with @name to hand off or ask them; use @all to ask everyone. If you have nothing "
    "useful to add, reply exactly: " + NO_REPLY
)


def _which(cmd: str) -> str | None:
    """Find a CLI even when the service PATH lacks ~/.local/bin."""
    extra = os.pathsep.join([str(Path.home() / ".local/bin"), "/usr/local/bin"])
    return shutil.which(cmd, path=os.environ.get("PATH", "") + os.pathsep + extra)


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def call_api(spec: dict, system: str, messages: list[dict]) -> str:
    """One chat call. messages: [{"role": "user"|"assistant", "content": str}]."""
    base = spec.get("base_url", "").rstrip("/")
    key = spec.get("api_key", "")
    if spec.get("api_format") == "anthropic":
        payload = {"model": spec.get("model", ""), "max_tokens": 2048, "system": system,
                   "messages": messages}
        if spec.get("temperature") is not None:
            payload["temperature"] = float(spec["temperature"])
        data = _post_json(f"{base}/messages", payload,
                          {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    payload = {"model": spec.get("model", ""),
               "messages": [{"role": "system", "content": system}] + messages}
    if spec.get("temperature") is not None:
        payload["temperature"] = float(spec["temperature"])
    data = _post_json(f"{base}/chat/completions", payload,
                      {"Authorization": f"Bearer {key}"} if key else {})
    return data["choices"][0]["message"].get("content") or ""


def list_models(base_url: str, api_key: str, api_format: str) -> list[str]:
    base = base_url.rstrip("/")
    headers = ({"x-api-key": api_key, "anthropic-version": "2023-06-01"} if api_format == "anthropic"
               else ({"Authorization": f"Bearer {api_key}"} if api_key else {}))
    req = urllib.request.Request(f"{base}/models", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))


def cli_command(spec: dict, prompt: str, session: str | None, system: str) -> list[str]:
    kind, perm = spec["kind"], spec.get("permissions", "read")
    exe = _which(spec.get("command") or kind) or kind
    if kind == "claude":
        cmd = [exe, "-p", prompt, "--output-format", "json", "--append-system-prompt", system]
        if session:
            cmd += ["--resume", session]
        if perm == "full":
            cmd += ["--dangerously-skip-permissions"]
        elif perm == "edit":
            cmd += ["--permission-mode", "acceptEdits"]
        else:
            # -p mode denies anything not allowed; no prompt can appear
            cmd += ["--allowedTools", "Read", "Grep", "Glob", "LS"]
    else:  # agy: no system-prompt flag, so the room prompt rides in the first turn
        if session and spec.get("persona"):  # persona can change mid-session: restate it
            prompt = f"(Your current persona: {spec['persona']})\n\n{prompt}"
        cmd = [exe, "-p", prompt if session else f"{system}\n\n{prompt}", "--output-format", "json"]
        if session:
            cmd += ["--conversation", session]
        if perm == "full":
            cmd += ["--dangerously-skip-permissions"]
        else:
            cmd += ["--mode", "accept-edits" if perm == "edit" else "plan"]
    if spec.get("model"):
        cmd += ["--model", spec["model"]]
    return cmd


def parse_cli_output(out: str) -> tuple[str, str | None]:
    """-> (reply text, session id). Tolerates plain text output."""
    try:
        data = json.loads(out.strip().splitlines()[-1] if out.strip() else "{}")
    except (json.JSONDecodeError, IndexError):
        return out.strip(), None
    if not isinstance(data, dict):
        return out.strip(), None
    text = next((data[k] for k in ("result", "response", "text", "output") if isinstance(data.get(k), str)), "")
    sess = next((data[k] for k in ("session_id", "conversation_id", "conversationId", "sessionId")
                 if isinstance(data.get(k), str)), None)
    return text.strip(), sess


class Runner:
    """One managed agent: presence heartbeat + trigger loop, all in-process."""

    def __init__(self, manager: "AgentManager", name: str):
        self.m = manager
        self.name = name
        self.stop = threading.Event()
        self.working = False
        self.proc: subprocess.Popen | None = None  # in-flight CLI turn, killed on shutdown

    @property
    def spec(self) -> dict:
        return self.m.specs[self.name]  # re-read each turn: edits apply immediately

    def start(self):
        hub = self.m.hub
        spec = self.spec
        hub.registry.seed({self.name: {"label": spec.get("label") or self.name,
                                       "color": spec.get("color") or "#8b5cf6", "type": spec["kind"]}})
        if not hub.registry.get_instance(self.name):
            hub.registry.register(self.name, spec.get("label") or None)
        threading.Thread(target=self._heartbeat, daemon=True, name=f"hb-{self.name}").start()
        threading.Thread(target=self._loop, daemon=True, name=f"agent-{self.name}").start()

    def shutdown(self):
        self.stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        try:
            self.m.hub.registry.deregister(self.name)
        except Exception:
            log.exception("deregister %s", self.name)

    def _heartbeat(self):
        import mcp_bridge
        while not self.stop.is_set():
            mcp_bridge._touch_presence(self.name)
            mcp_bridge.set_active(self.name, self.working)
            self.stop.wait(2)

    def _loop(self):
        qf = Path(self.m.data_dir) / f"{self.name}_queue.jsonl"
        if qf.exists():
            qf.write_text("", "utf-8")  # don't replay triggers from before a restart
        while not self.stop.is_set():
            channels = []
            try:
                if qf.exists() and qf.stat().st_size > 0:
                    lines = qf.read_text("utf-8").splitlines()
                    qf.write_text("", "utf-8")
                    for line in lines:
                        try:
                            ch = json.loads(line).get("channel", "general")
                        except (json.JSONDecodeError, AttributeError):
                            ch = "general"
                        if ch not in channels:
                            channels.append(ch)
            except OSError:
                log.exception("queue read %s", self.name)
            for ch in channels:
                self._turn(ch)
            self.stop.wait(1)

    def _addressed(self, channel: str) -> bool:
        """Was the latest message from someone else aimed at this agent by name?
        (@all reaches everyone, but each agent may judge it isn't theirs to answer.)"""
        for m in reversed(self.m.hub.store.get_recent(20, channel=channel)):
            if m.get("type", "chat") == "chat" and m.get("sender") != self.name:
                return bool(re.search(rf"@{re.escape(self.name)}(?![\w-])", m.get("text", ""), re.I))
        return False

    def _system(self, channel: str = "general") -> str:
        import mcp_bridge
        parts = [ROOM_PROMPT.format(name=self.name), PERSONA_HELP]
        persona = self.spec.get("persona")
        parts.append(f"Your persona (written by you):\n{persona}" if persona else
                     "You have no persona yet. Define one, distinct from the other agents, the first "
                     "time you're asked to or when it fits.")
        if not self._addressed(channel):
            parts.append(
                "The latest message did not name you; it went to everyone (@all). Answer only if it asks all "
                "participants, concerns your role, or concerns work you did or know about. Otherwise "
                f"reply exactly {NO_REPLY} (it is not posted). Don't repeat what another agent already said.")
        role = mcp_bridge.get_role(self.name)
        if role:
            parts.append(f"Your role in the room: {role}.")
        if self.spec.get("system_prompt"):
            parts.append(self.spec["system_prompt"])
        try:
            rules = self.m.hub.rules.active_list().get("rules", [])
        except Exception:
            rules = []
        if rules:
            parts.append("Room rules: " + "; ".join(rules))
        others = [n for n in self.m.hub.registry.get_active_names()
                  if n != self.name and self.m.in_channel(n, channel)]
        if others:
            lines = []
            for n in others:
                p = (self.m.specs.get(n, {}).get("persona") or "").strip().replace("\n", " ")
                lines.append(f"@{n}" + (f": {p[:200]}" if p else " (no persona yet)"))
            parts.append("Other agents in this channel:\n" + "\n".join(lines) + "\nYour persona and any "
                         "name you choose must be distinct from theirs and from names already used in the chat.")
        return "\n\n".join(parts)

    def _turn(self, channel: str):
        # One server agent at a time per channel: each turn starts from the latest
        # transcript, so agents answering the same @all see and react to each other.
        with self.m.channel_lock(channel):
            self._turn_locked(channel)

    def _turn_locked(self, channel: str):
        hub = self.m.hub
        self.working = True
        try:
            spec = self.spec
            if spec["kind"] == "api":
                reply = self._api_turn(channel)
            else:
                reply = self._cli_turn(channel)
            reply = (reply or "").strip()
            found = PERSONA_RE.findall(reply)
            if found:
                self.m.set_persona(self.name, found[-1].strip())
                reply = PERSONA_RE.sub("", reply).strip()
                hub.store.add("system", f"{self.name} updated its persona.", msg_type="system",
                              channel=channel)
            renames = RENAME_RE.findall(reply)
            reply = RENAME_RE.sub("", reply).strip()
            for prefix in (f"{self.name}: ", f"{self.name}:"):
                if reply.startswith(prefix):
                    reply = reply[len(prefix):].strip()
            if reply and reply != NO_REPLY:
                hub.store.add(self.name, reply, channel=channel)
            if renames:  # after posting, so the reply is re-attributed with the rest
                note = self.m.rename(self.name, renames[-1].strip())
                hub.store.add("system", note, msg_type="system", channel=channel)
        except Exception as exc:
            log.exception("turn failed for %s", self.name)
            hub.store.add("system", f"{self.name} failed: {str(exc)[:200]}", msg_type="system",
                          channel=channel)
        finally:
            self.working = False

    def _api_turn(self, channel: str) -> str:
        n = int(self.spec.get("context_messages") or 30)
        msgs = []
        start = int(self.spec.get("context_from") or 0)
        for m in self.m.hub.store.get_recent(n, channel=channel):
            if m["id"] < start:
                continue  # spawned instance: its conversation starts at spawn time
            if m.get("sender") in ("system", "") or m.get("type", "chat") != "chat":
                continue
            if m["sender"] == self.name:
                msgs.append({"role": "assistant", "content": m["text"]})
            else:
                msgs.append({"role": "user", "content": f"{m['sender']}: {m['text']}"})
        if not msgs or msgs[0]["role"] != "user":  # Anthropic requires a user turn first
            msgs.insert(0, {"role": "user", "content": "(earlier conversation omitted)"})
        return call_api(self.spec, self._system(channel), msgs)

    def _cli_turn(self, channel: str) -> str:
        spec = self.spec
        state = self.m.state.setdefault(self.name, {"sessions": {}, "cursors": {}})
        session = state["sessions"].get(channel)
        cursor = state["cursors"].get(channel, 0)
        store = self.m.hub.store
        new = store.get_since(cursor, channel=channel) if session else \
            store.get_recent(int(spec.get("context_messages") or 30), channel=channel)
        start = int(spec.get("context_from") or 0)
        new = [m for m in new if m.get("type", "chat") == "chat" and m.get("sender") != self.name
               and m["id"] >= start]
        if not new:
            return ""
        transcript = "\n".join(f"{m['sender']}: {m['text']}" for m in new)
        prompt = f"New messages in #{channel}:\n\n{transcript}\n\nYour reply to post in #{channel}:"
        cwd = os.path.expanduser(spec.get("cwd") or str(Path.home()))
        cmd = cli_command(spec, prompt, session, self._system(channel))
        self.proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, text=True)
        try:
            stdout, stderr = self.proc.communicate(timeout=CLI_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            stdout, stderr = self.proc.communicate()
        finally:
            code, self.proc = self.proc.returncode, None
        if self.stop.is_set():
            return ""  # agent was removed mid-turn: post nothing
        text, new_session = parse_cli_output(stdout)
        if code != 0 and not text:
            raise RuntimeError((stderr or stdout or f"exit {code}").strip()[-300:])
        state["cursors"][channel] = max(m["id"] for m in new)
        if new_session:
            state["sessions"][channel] = new_session
        self.m.save_state()
        return text


class AgentManager:
    def __init__(self, hub, data_dir: str):
        self.hub = hub
        self.data_dir = data_dir
        self.path = Path(data_dir) / "managed_agents.json"
        self.state_path = Path(data_dir) / "managed_agents_state.json"
        self.lock = threading.Lock()
        self.specs: dict[str, dict] = {}
        self.state: dict[str, dict] = {}
        self.runners: dict[str, Runner] = {}
        self._channel_locks: dict[str, threading.Lock] = {}
        # Channel membership for every agent (managed or not): #general holds
        # everyone; other channels hold only the agents added to them.
        self.members_path = Path(data_dir) / "channel_members.json"
        self.members: dict[str, list[str]] = {}
        if self.members_path.exists():
            self.members = json.loads(self.members_path.read_text("utf-8"))
        if self.path.exists():
            self.specs = {s["name"]: s for s in json.loads(self.path.read_text("utf-8"))}
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text("utf-8"))

    def _write(self, path: Path, data):
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    def save(self):
        self._write(self.path, list(self.specs.values()))

    def save_state(self):
        with self.lock:
            self._write(self.state_path, self.state)

    def _name_taken(self, name: str) -> bool:
        """In use as an agent, or as a registry family that still has members (a renamed
        agent keeps its original family; reusing that name would renumber it)."""
        reg = self.hub.registry
        return bool(name in self.specs or reg.get_instance(name) or reg.get_instances_for(name))

    def channel_lock(self, channel: str) -> threading.Lock:
        with self.lock:
            return self._channel_locks.setdefault(channel, threading.Lock())

    def in_channel(self, name: str, channel: str) -> bool:
        return channel == "general" or name in self.members.get(channel, [])

    def channels_of(self, name: str) -> list[str]:
        return ["general"] + sorted(ch for ch, names in self.members.items() if name in names and ch != "general")

    def set_member(self, name: str, channel: str, on: bool):
        if channel == "general":
            return  # everyone is always in #general
        with self.lock:
            names = [n for n in self.members.get(channel, []) if n != name]
            if on:
                names.append(name)
            self.members[channel] = names
            self._write(self.members_path, self.members)

    def forget_member(self, name: str):
        for ch in list(self.members):
            self.set_member(name, ch, False)

    @staticmethod
    def public(spec: dict) -> dict:
        out = {k: v for k, v in spec.items() if k != "api_key"}
        out["has_api_key"] = bool(spec.get("api_key"))
        return out

    def list(self) -> list[dict]:
        return [dict(self.public(s), running=s["name"] in self.runners,
                     channels=[c for c in self.channels_of(s["name"]) if c != "general"])
                for s in self.specs.values()]

    def validate(self, spec: dict) -> str | None:
        import re
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", spec.get("name", "")):
            return "name: 1-32 chars, lowercase letters, digits, - or _"
        if spec.get("kind") not in KINDS:
            return f"kind must be one of {KINDS}"
        if not isinstance(spec.get("channels", []), list):
            return "channels must be a list of channel names"
        if spec.get("permissions", "read") not in PERMISSIONS:
            return f"permissions must be one of {PERMISSIONS}"
        if spec["name"] not in self.specs and self._name_taken(spec["name"]):
            return f"an agent named '{spec['name']}' is already connected; pick another name"
        if spec["kind"] == "api" and not spec.get("base_url"):
            return "base_url is required for API agents"
        if spec["kind"] != "api" and not _which(spec.get("command") or spec["kind"]):
            return f"'{spec.get('command') or spec['kind']}' is not installed on this machine"
        return None

    def upsert(self, spec: dict) -> str | None:
        src = spec.pop("copy_key_from", None)
        if src:
            spec.pop("persona", None)  # duplicates get their own persona
        spec = {k: v for k, v in spec.items() if k not in ("running", "has_api_key", "duplicateOf")}
        old = self.specs.get(spec.get("name", "")) or self.specs.get(src or "", {})
        if not spec.get("api_key") and old.get("api_key"):
            spec["api_key"] = old["api_key"]  # blank in the form = keep the stored key
        spec.setdefault("permissions", "read")
        err = self.validate(spec)
        if err:
            return err
        chans = spec.pop("channels", None)  # membership lives in channel_members.json
        self.specs[spec["name"]] = spec
        self.save()
        if chans is not None:
            for ch in set(self.members) | set(chans):
                self.set_member(spec["name"], ch, ch in chans)
        self._apply(spec["name"])
        return None

    def spawn(self, source: str, channel: str, name: str | None = None) -> str:
        """New instance of a managed agent, pinned to `channel`, with fresh context:
        a new CLI session, or (API agents) only messages from now on. -> status text."""
        src = self.specs.get(source.lstrip("@").lower())
        if not src:
            return (f"No agent '{source}' runs on this server. Spawnable: "
                    + (", ".join(sorted(self.specs)) or "none; add one in Settings > Agents"))
        if not name:
            n = 2
            while self._name_taken(f"{src['name']}-{n}"):
                n += 1
            name = f"{src['name']}-{n}"
        # persona and memory are per instance: the new one defines its own
        spec = {k: v for k, v in src.items() if k not in ("label", "channels", "persona")}
        spec.update(name=name.lstrip("@").lower(), channels=[channel], enabled=True, spawned=True,
                    context_from=self.hub.store.last_id + 1)
        err = self.upsert(spec)
        return err or (f"Spawned @{spec['name']} (new instance of @{src['name']}) in #{channel}, "
                       f"starting with fresh context. Remove it with /despawn {spec['name']}.")

    def set_persona(self, name: str, text: str):
        if name in self.specs:
            self.specs[name]["persona"] = text[:1200]
            with self.lock:
                self.save()

    def fresh(self, name: str) -> str:
        """New context, same persona: drop CLI sessions, start the conversation now."""
        name = name.lstrip("@").lower()
        if name not in self.specs:
            return f"No agent '{name}' runs on this server."
        with self.lock:
            self.state.pop(name, None)
            self.specs[name]["context_from"] = self.hub.store.last_id + 1
            self.save()
        self.save_state()
        return f"@{name} starts with a fresh context (its persona is kept)."

    def rename(self, old: str, wanted: str) -> str:
        """Change an agent's @handle everywhere; persona, memory and channels move with it."""
        old = old.lstrip("@").lower()
        label = wanted.strip().lstrip("@")[:40]
        new = re.sub(r"[^a-z0-9_-]", "", label.lower().replace(" ", "-"))[:32]
        import mcp_bridge
        if old not in self.specs:
            return f"No agent '{old}' runs on this server."
        if not new or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", new):
            return f"'{wanted}' can't be a handle (lowercase letters, digits, - or _)."
        if new == old:
            return f"@{old} already has that handle."
        taken = (self._name_taken(new)
                 or new in {"user", "system", "all", "both", "everyone", "here", "me"}
                 or new == str(getattr(self.hub, "room_settings", {}).get("username", "")).lower())
        if taken:
            return f"@{new} is taken; @{old} keeps its handle."
        runner = self.runners.pop(old, None)
        if runner:
            runner.stop.set()  # keep the registry instance: it is renamed below
        mcp_bridge.migrate_identity(old, new)
        result = self.hub.registry.rename(old, new, label)
        if isinstance(result, str) and self.hub.registry.get_instance(old):
            if runner:
                self.runners[old] = Runner(self, old)
                self.runners[old].start()
            return f"Rename failed: {result}"
        self.hub.store.rename_sender(old, new)
        with self.lock:
            spec = self.specs.pop(old)
            spec.update(name=new, label=label)
            self.specs[new] = spec
            if old in self.state:
                self.state[new] = self.state.pop(old)
            for names in self.members.values():
                if old in names:
                    names[names.index(old)] = new
            self.save()
            self._write(self.members_path, self.members)
        self.save_state()
        (Path(self.data_dir) / f"{old}_queue.jsonl").unlink(missing_ok=True)
        if spec.get("enabled", True):
            self.runners[new] = Runner(self, new)
            self.runners[new].start()
        return f"@{old} is now @{new} ({label})."

    def channel_deleted(self, channel: str) -> list[str]:
        """Drop the channel's membership and sessions. Spawned instances that lived only
        there are deleted (runner stopped, CLI process killed). -> deleted names."""
        doomed = [n for n in self.members.get(channel, [])
                  if self.specs.get(n, {}).get("spawned") and self.channels_of(n) == ["general", channel]]
        for name in doomed:
            self.delete(name)
        with self.lock:
            self.members.pop(channel, None)
            self._write(self.members_path, self.members)
            for st in self.state.values():
                st.get("sessions", {}).pop(channel, None)
                st.get("cursors", {}).pop(channel, None)
        self.save_state()
        return doomed

    def channel_renamed(self, old: str, new: str):
        with self.lock:
            if old in self.members:
                self.members[new] = self.members.pop(old)
                self._write(self.members_path, self.members)
            for st in self.state.values():
                for key in ("sessions", "cursors"):
                    if old in st.get(key, {}):
                        st[key][new] = st[key].pop(old)
        self.save_state()

    def despawn(self, name: str) -> str:
        name = name.lstrip("@").lower()
        spec = self.specs.get(name)
        if not spec:
            return f"No agent '{name}' to remove."
        if not spec.get("spawned"):
            return f"@{name} wasn't created with /spawn; remove it in Settings > Agents."
        self.delete(name)
        return f"Removed @{name}."

    def delete(self, name: str):
        self.forget_member(name)
        self.specs.pop(name, None)
        self.state.pop(name, None)
        self.save()
        self.save_state()
        self._apply(name)

    def _apply(self, name: str):
        """Start/stop the runner to match the spec. Field edits need no restart."""
        spec = self.specs.get(name)
        want = bool(spec and spec.get("enabled", True))
        runner = self.runners.get(name)
        if runner and not want:
            self.runners.pop(name).shutdown()
        elif want and not runner:
            self.runners[name] = Runner(self, name)
            self.runners[name].start()

    def start_all(self):
        for name in list(self.specs):
            self._apply(name)
