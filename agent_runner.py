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

import base64
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
API_TIMEOUT = 600          # local reasoning models can think for minutes; per-agent "timeout" overrides

PERSONA_RE = re.compile(r"\[persona\](.*?)\[/persona\]", re.S | re.I)
RESUME_NOTE = ("(Your previous turn here was cut off by a server restart before you could post. "
               "Check what you already did, such as files changed, commands run or commits, then "
               "carry on from there instead of starting over.)")
ATTACH_RE = re.compile(r"\[attach:\s*([^\]\n]+)\]", re.I)
MAX_ATTACH_BYTES = 25 * 1024 * 1024
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
    "agent with @name to hand off work or ask them; use @all to ask everyone. Agents work at "
    "the same time: if the agent you mention is busy, your request waits in its queue and it "
    "picks it up when it finishes, so don't stall waiting for an answer. To show the owner an "
    "image or file (screenshot, render, document), put [attach: /absolute/path] in your reply; "
    "it is posted as an attachment they can open. If you have nothing "
    "useful to add, reply exactly: " + NO_REPLY
)


def _which(cmd: str) -> str | None:
    """Find a CLI even when the service PATH lacks ~/.local/bin."""
    extra = os.pathsep.join([str(Path.home() / ".local/bin"), "/usr/local/bin"])
    return shutil.which(cmd, path=os.environ.get("PATH", "") + os.pathsep + extra)


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # The body says why (bad model id, missing field, context too long...): surface it
        exc.msg = f"{exc.msg}: {exc.read()[:400].decode('utf-8', 'replace')}"
        raise


MAX_TOOL_STEPS = 40        # tool rounds per turn for API agents; per-agent "max_tool_steps" overrides
MAX_IMAGES = 3              # newest images sent inline to API agents per turn
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _to_anthropic(content):
    """OpenAI-style content parts -> Anthropic blocks (text stays a string)."""
    if isinstance(content, str):
        return content
    out = []
    for p in content:
        if p.get("type") == "image_url":
            head, data = p["image_url"]["url"].split(",", 1)
            out.append({"type": "image", "source": {"type": "base64", "data": data,
                                                    "media_type": head[5:].split(";")[0]}})
        else:
            out.append({"type": "text", "text": p.get("text", "")})
    return out


def _timeout(spec: dict) -> int:
    try:
        return max(10, int(spec.get("timeout") or API_TIMEOUT))
    except (TypeError, ValueError):
        return API_TIMEOUT


def call_api(spec: dict, system: str, messages: list[dict]) -> str:
    return call_api_message(spec, system, messages).get("content") or ""


def call_api_message(spec: dict, system: str, messages: list[dict], tools: list | None = None,
                     tool_choice: str | None = None) -> dict:
    """One chat call -> the assistant message ({"content", "tool_calls"?}).
    Tools are passed for OpenAI-compatible providers only."""
    base = spec.get("base_url", "").rstrip("/")
    key = spec.get("api_key", "")
    if spec.get("api_format") == "anthropic":
        messages = [dict(m, content=_to_anthropic(m["content"])) for m in messages]
        payload = {"model": spec.get("model", ""), "max_tokens": 2048, "system": system,
                   "messages": messages}
        if spec.get("temperature") is not None:
            payload["temperature"] = float(spec["temperature"])
        data = _post_json(f"{base}/messages", payload,
                          {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=_timeout(spec))
        return {"content": "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")}
    payload = {"model": spec.get("model", ""),
               "messages": [{"role": "system", "content": system}] + messages}
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    if spec.get("temperature") is not None:
        payload["temperature"] = float(spec["temperature"])
    data = _post_json(f"{base}/chat/completions", payload,
                      {"Authorization": f"Bearer {key}"} if key else {}, timeout=_timeout(spec))
    return data["choices"][0]["message"]


def list_models(base_url: str, api_key: str, api_format: str) -> list[str]:
    base = base_url.rstrip("/")
    headers = ({"x-api-key": api_key, "anthropic-version": "2023-06-01"} if api_format == "anthropic"
               else ({"Authorization": f"Bearer {api_key}"} if api_key else {}))
    req = urllib.request.Request(f"{base}/models", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))


def cli_command(spec: dict, prompt: str, session: str | None, system: str,
                protect: str | None = None, mcp: tuple[str | None, list[str]] = (None, [])) -> list[str]:
    kind, perm = spec["kind"], spec.get("permissions", "read")
    exe = _which(spec.get("command") or kind) or kind
    if kind == "claude":
        cmd = [exe, "-p", prompt, "--output-format", "json", "--append-system-prompt", system]
        if protect:  # AgentHub's data dir holds secrets and keys: keep file tools out of it
            cmd += ["--disallowedTools", f"Read({protect}/**)", f"Edit({protect}/**)", f"Write({protect}/**)"]
        if session:
            cmd += ["--resume", session]
        extra = [f"Bash({c}:*)" for c in spec.get("allowed_commands") or []]  # e.g. Bash(slop-chop:*)
        mcp_path, mcp_rules = mcp
        if mcp_path:  # MCP servers granted in Settings; their tools are allowed at every level
            cmd += ["--mcp-config", mcp_path]
            extra += mcp_rules
        if perm == "full":
            cmd += ["--dangerously-skip-permissions"]
        elif perm == "edit":
            cmd += ["--permission-mode", "acceptEdits"] + (["--allowedTools", *extra] if extra else [])
        else:
            # -p mode denies anything not allowed; no prompt can appear
            cmd += ["--allowedTools", "Read", "Grep", "Glob", "LS", *extra]
    else:  # agy: no system-prompt flag, so the room prompt rides in the first turn
        if session and spec.get("persona"):  # persona can change mid-session: restate it
            prompt = f"(Your current persona: {spec['persona']})\n\n{prompt}"
        cmd = [exe, "-p", prompt if session else f"{system}\n\n{prompt}", "--output-format", "json"]
        if session:
            cmd += ["--conversation", session]
        # agy's own ~/.gemini/antigravity-cli/settings.json can deny command/write_file/mcp,
        # and deny rules beat --dangerously-skip-permissions. --settings is session-only
        # (never saved), so the room's permission switch wins without touching that file.
        if perm == "full":
            cmd += ["--dangerously-skip-permissions", "--settings", json.dumps({"permissions": {
                "allow": ["read_url(*)", "command(*)", "write_file(*)", "mcp(*)", "execute_url(*)",
                          "unsandboxed(*)"], "deny": []}})]
        elif perm == "edit":
            cmd += ["--mode", "accept-edits", "--settings", json.dumps({"permissions": {
                "allow": ["read_url(*)", "write_file(*)"],
                "deny": ["command(*)", "mcp(*)", "execute_url(*)", "unsandboxed(*)"]}})]
        else:
            cmd += ["--mode", "plan"]
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
        self.resuming: set[str] = set()  # channels whose last turn a restart cut off

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
        # ...but do finish turns a restart interrupted (recorded in the state file)
        pending = list(self.m.state.get(self.name, {}).get("inflight", []))
        for ch in pending:
            self.resuming.add(ch)
            self.m.hub.store.add("system", f"@{self.name}'s turn was cut off by a server restart; resuming it.",
                                 msg_type="system", channel=ch)
        while not self.stop.is_set():
            channels, pending = pending, []
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

    def _files(self, m: dict) -> list[tuple[str, dict]]:
        """(absolute path, attachment) for each file on a message."""
        updir = Path(getattr(self.m.hub.store, "upload_dir", "uploads")).resolve()
        return [(str(updir / Path(a.get("url", "")).name), a) for a in m.get("attachments") or []
                if a.get("url")]

    def _text(self, m: dict) -> str:
        """Message text plus on-disk paths of its attachments (agents read them by path)."""
        extra = [f"[attached {'image' if a.get('kind') == 'image' else 'file'} "
                 f"{a.get('name', '')}: {path}]" for path, a in self._files(m)]
        return "\n".join([m.get("text", "")] + extra).strip()

    def _attachments(self, paths: list[str]) -> tuple[list[dict], list[str]]:
        """Copy files an agent attached into uploads/ -> (attachment dicts, problems)."""
        import mimetypes
        import shutil
        import uuid
        updir = Path(getattr(self.m.hub.store, "upload_dir", "uploads"))
        cwd = Path(os.path.expanduser(self.spec.get("cwd") or "~"))
        out, problems = [], []
        for raw in paths:
            src = (cwd / os.path.expanduser(raw.strip().strip("`'\""))).resolve()
            if not src.is_file():
                problems.append(f"(couldn't attach {raw.strip()}: file not found)")
                continue
            if src.stat().st_size > MAX_ATTACH_BYTES:
                problems.append(f"(couldn't attach {src.name}: larger than 25 MB)")
                continue
            updir.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}{src.suffix.lower()}"
            shutil.copyfile(src, updir / name)
            mt = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
            out.append({"name": src.name, "url": f"/uploads/{name}", "size": src.stat().st_size,
                        "media_type": mt, "kind": "image" if mt.startswith("image/") else "file"})
        return out, problems

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
        try:
            import admin_api
            granted = admin_api.secret_names_for(self.name)
        except Exception:
            granted = []
        if self.spec.get("kind") == "api" and self.spec.get("api_format") != "anthropic":
            import agent_tools
            sk = agent_tools.skills()
            perm = self.spec.get("permissions", "read")
            allowed = self.spec.get("allowed_commands") or []
            parts.append(
                f"You can use tools (permission level: {perm}"
                + (f"; allowed programs: {', '.join(allowed)}" if allowed and perm != "full" else "")
                + f"; working directory: {self.spec.get('cwd') or '~'}). Installed skills (call load_skill "
                "before using one):\n" + "\n".join(f"- {n}: {v['description']}" for n, v in sorted(sk.items())))
        try:
            import rulebooks
            rb = rulebooks.prompt_for(self.name, channel)
        except Exception:
            rb = ""
        if rb:
            parts.append(rb)
        if granted:
            parts.append("Credentials granted to you by the owner, available as environment variables: "
                         + ", ".join(granted) + ". Other agents do not have them; never print their values.")
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
        # Agents in a channel run at the same time (live chat). Each agent still takes its
        # own triggers one at a time: messages for a busy agent wait in its queue.
        hub = self.m.hub
        self.working = True
        self.m.set_inflight(self.name, channel, True)  # survives a restart: resumed at startup
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
            attachments, problems = self._attachments(ATTACH_RE.findall(reply))
            reply = ATTACH_RE.sub("", reply).strip()
            if problems:
                reply = (reply + "\n\n" + "\n".join(problems)).strip()
            renames = RENAME_RE.findall(reply)
            reply = RENAME_RE.sub("", reply).strip()
            for prefix in (f"{self.name}: ", f"{self.name}:"):
                if reply.startswith(prefix):
                    reply = reply[len(prefix):].strip()
            if (reply and reply != NO_REPLY) or attachments:
                hub.store.add(self.name, "" if reply == NO_REPLY else reply, channel=channel,
                              attachments=attachments)
            else:
                log.info("%s stayed quiet in #%s (%s)", self.name, channel, "no reply" if reply else "empty answer")
            if renames:  # after posting, so the reply is re-attributed with the rest
                note = self.m.rename(self.name, renames[-1].strip())
                hub.store.add("system", note, msg_type="system", channel=channel)
        except Exception as exc:
            log.exception("turn failed for %s", self.name)
            why = str(exc)[:200]
            if isinstance(exc, TimeoutError) or "timed out" in why:
                why = (f"no answer within {_timeout(self.spec)} s (the model may still have been "
                       "generating; raise Timeout in Settings > Agents)")
            hub.store.add("system", f"{self.name} failed: {why}", msg_type="system", channel=channel)
        finally:
            self.working = False
            if not self.stop.is_set():  # a shutdown mid-turn keeps the mark so it resumes
                self.m.set_inflight(self.name, channel, False)
            self.resuming.discard(channel)

    def _api_turn(self, channel: str) -> str:
        n = int(self.spec.get("context_messages") or 30)
        start = int(self.spec.get("context_from") or 0)
        history = [m for m in self.m.hub.store.get_recent(n, channel=channel)
                   if m["id"] >= start  # spawned/fresh instance: conversation starts there
                   and m.get("sender") not in ("system", "") and m.get("type", "chat") == "chat"]
        if history and history[-1]["sender"] == self.name:
            # Nothing new since our own reply (a trigger we already answered). Gemini also
            # rejects a request that ends with the model's turn (HTTP 400).
            return NO_REPLY
        inline = set()  # ids of the newest messages whose images go inline
        for m in reversed(history):
            if len(inline) >= MAX_IMAGES:
                break
            if any(a.get("kind") == "image" for _, a in self._files(m)) and m["sender"] != self.name:
                inline.add(m["id"])

        def build(with_images: bool) -> list[dict]:
            msgs = []
            for m in history:
                if m["sender"] == self.name:
                    msgs.append({"role": "assistant", "content": m["text"]})
                    continue
                text = f"{m['sender']}: {self._text(m)}"
                parts = []
                if with_images and m["id"] in inline:
                    for path, a in self._files(m):
                        try:
                            if a.get("kind") == "image" and os.path.getsize(path) <= MAX_IMAGE_BYTES:
                                data = base64.b64encode(Path(path).read_bytes()).decode()
                                parts.append({"type": "image_url", "image_url": {
                                    "url": f"data:{a.get('media_type', 'image/png')};base64,{data}"}})
                        except OSError:
                            pass
                msgs.append({"role": "user", "content": [{"type": "text", "text": text}] + parts if parts else text})
            if not msgs or msgs[0]["role"] != "user":  # Anthropic requires a user turn first
                msgs.insert(0, {"role": "user", "content": "(earlier conversation omitted)"})
            return msgs

        msgs = build(with_images=bool(inline))
        if channel in self.resuming:
            msgs.append({"role": "user", "content": RESUME_NOTE})
        try:
            return self._tool_loop(channel, msgs, images_inline=bool(inline))
        except urllib.error.HTTPError as exc:
            if not inline or exc.code not in (400, 415, 422):
                raise
            log.info("%s: model rejected images (%s); retrying text-only", self.name, exc.code)
            return self._tool_loop(channel, build(with_images=False))

    def _tool_loop(self, channel: str, msgs: list[dict], images_inline: bool = False) -> str:
        """Let an API agent call tools (agent_tools) until it answers in text."""
        import admin_api
        import agent_tools
        spec, system = self.spec, self._system(channel)
        perm, allowed = spec.get("permissions", "read"), spec.get("allowed_commands") or []
        import mcp_hub
        tools, mcp_names = None, {}
        if spec.get("api_format") != "anthropic":
            mcp_specs, mcp_names = mcp_hub.tool_specs_for(self.name)
            tools = agent_tools.tool_specs(perm, allowed) + mcp_specs
        env = dict(os.environ)
        env.update(admin_api.secrets_for(self.name))
        runner = agent_tools.ToolRunner(spec.get("cwd") or "~", perm, allowed, env,
                                        protect=str(Path(self.m.data_dir).resolve()))
        try:
            steps = max(1, int(spec.get("max_tool_steps") or MAX_TOOL_STEPS))
        except (TypeError, ValueError):
            steps = MAX_TOOL_STEPS
        for step in range(steps):
            try:  # "require_tool": the first step must call a tool (read before answering)
                reply = call_api_message(spec, system, msgs, tools,
                                         "required" if step == 0 and spec.get("require_tool") else None)
            except urllib.error.HTTPError as exc:
                # A 400 with inline images: let the caller retry without images first
                if images_inline or not tools or exc.code not in (400, 422):
                    raise
                log.info("%s: provider rejected tools (%s); answering without them", self.name, exc.code)
                tools = None
                continue
            calls = reply.get("tool_calls") or []
            text = reply.get("content") or ""
            if not calls and tools and "<tool_call>" in text and not self.stop.is_set():
                # Tool-call markup as plain text: the server didn't parse it (often a tool name
                # we never declared, e.g. Claude Code's "read"/"grep"). Don't post it; correct it.
                names = ", ".join(t["function"]["name"] for t in tools)
                log.info("%s wrote tool calls as text; asking it to use its real tools", self.name)
                msgs.append({"role": "assistant", "content": text})
                msgs.append({"role": "user", "content": (
                    "(That reply had tool-call markup as plain text, so nothing ran and it was not "
                    f"posted. Your only tools are: {names}. Call them with those exact names, or "
                    "answer in plain text. There is no separate grep or read tool.)")})
                continue
            if not calls or self.stop.is_set():
                return text
            # keep reasoning_details: Gemini (via OpenRouter) needs its thought signatures back
            msgs.append({k: v for k, v in reply.items() if k in ("content", "tool_calls", "reasoning_details")}
                        | {"role": "assistant", "content": text, "tool_calls": calls})
            for c in calls:
                fn = c.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                log.info("%s tool %s %s", self.name, fn.get("name"), str(args)[:120])
                args = args if isinstance(args, dict) else {}
                if fn.get("name") in mcp_names:
                    srv, tool = mcp_names[fn["name"]]
                    try:
                        out = mcp_hub.call_tool(srv, tool, args)[:agent_tools.MAX_OUT]
                    except BaseException as exc:  # connection errors arrive as ExceptionGroups
                        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                            raise
                        out = f"error: MCP server {srv['name']} failed: {str(exc)[:300]}"
                else:
                    out = runner.run(fn.get("name", ""), args)
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
        # Out of steps: one last call without tools so the agent reports where it got to
        log.info("%s used all %d tool steps; asking for a summary", self.name, steps)
        msgs.append({"role": "user", "content": (
            f"(You have used all {steps} tool steps for this turn. Don't call tools. Post what you did, "
            "what you found, and exactly what is left, so the work can continue next turn.)")})
        return call_api_message(spec, system, msgs, None).get("content") or \
            f"(stopped after {steps} tool steps without a summary)"

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
        transcript = "\n".join(f"{m['sender']}: {self._text(m)}" for m in new)
        hint = ("\n\n(Attached files are on disk at the paths shown; open them with your file-reading "
                "tool to see them.)" if "[attached " in transcript else "")
        prompt = f"New messages in #{channel}:\n\n{transcript}{hint}\n\nYour reply to post in #{channel}:"
        if channel in self.resuming:
            prompt = RESUME_NOTE + "\n\n" + prompt
        cwd = os.path.expanduser(spec.get("cwd") or str(Path.home()))
        import admin_api
        import mcp_hub
        cmd = cli_command(spec, prompt, session, self._system(channel),
                          protect=str(Path(self.m.data_dir).resolve()),
                          mcp=mcp_hub.claude_config(self.name, self.m.data_dir) if spec["kind"] == "claude" else (None, []))
        env = dict(os.environ)
        env.update(admin_api.secrets_for(self.name))  # only the secrets granted to this agent
        self.proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, text=True, env=env)
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

    def set_inflight(self, name: str, channel: str, on: bool):
        with self.lock:
            st = self.state.setdefault(name, {"sessions": {}, "cursors": {}})
            chans = [c for c in st.get("inflight", []) if c != channel] + ([channel] if on else [])
            if chans:
                st["inflight"] = chans
            else:
                st.pop("inflight", None)
        self.save_state()

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
        if not isinstance(spec.get("allowed_commands", []), list) or not all(
                re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", str(c)) for c in spec.get("allowed_commands", [])):
            return "allowed commands: program names only (letters, digits, . _ + -)"
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
        import admin_api
        import rulebooks
        admin_api.rename_grants(old, new)
        rulebooks.rename_agent(old, new)
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
        import admin_api
        import rulebooks
        admin_api.rename_grants(name, None)
        rulebooks.rename_agent(name, None)
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
