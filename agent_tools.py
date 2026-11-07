"""Tools and skills for API agents (LLMs reached over HTTP, e.g. Qwen on Strata).

CLI agents (Claude Code, agy) bring their own tools. API agents get this small
set through OpenAI-style function calling, run by the server on their behalf:

  load_skill   read an installed skill's instructions (index is in the prompt)
  read_file    read a text file            (every permission level)
  list_dir     list a directory            (every permission level)
  write_file   create/overwrite a file     (edit, full)
  run_command  run a command               (full: any shell command;
                                            otherwise only `allowed_commands`)

Skills are discovered from ~/.agents/skills and ~/.claude/skills, so anything
installed there shows up for every agent without config.

Security: tools run as the server's user. File tools refuse AgentHub's own data
folder (secrets, keys). With `full`, an agent can run anything this user can.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

SKILL_DIRS = [Path.home() / ".agents/skills", Path.home() / ".claude/skills"]
MAX_OUT = 12000
CMD_TIMEOUT = 600  # test suites and builds can take minutes


def skills() -> dict[str, dict]:
    """{name: {"description", "path"}} from SKILL.md front matter."""
    found = {}
    for d in SKILL_DIRS:
        for f in sorted(d.glob("*/SKILL.md")) if d.is_dir() else []:
            name, desc = f.parent.name, ""
            try:
                text = f.read_text("utf-8", errors="replace")
            except OSError:
                continue
            if text.startswith("---"):
                head = text.split("---", 2)[1]
                lines = head.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("name:"):
                        name = line[5:].strip() or name
                    if line.startswith("description:"):
                        val = line[12:].strip()
                        if val in (">", ">-", "|", "|-", ""):  # folded block: gather indented lines
                            val = " ".join(l.strip() for l in lines[i + 1:] if l.startswith((" ", "\t")))
                        desc = val
            found.setdefault(name, {"description": desc[:300], "path": str(f)})
    return found


def tool_specs(perm: str, allowed: list[str]) -> list[dict]:
    def fn(name, desc, props, required):
        return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
            "type": "object", "properties": props, "required": required}}}
    s = {"type": "string"}
    specs = [
        fn("load_skill", "Read the full instructions of an installed skill before using it.",
           {"name": s}, ["name"]),
        fn("read_file", "Read a text file. Relative paths start in your working directory.",
           {"path": s, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]),
        fn("list_dir", "List the entries of a directory.", {"path": s}, ["path"]),
    ]
    if perm in ("edit", "full"):
        specs.append(fn("write_file", "Create or overwrite a text file.", {"path": s, "content": s},
                        ["path", "content"]))
    if perm == "full" or allowed:
        which = "any shell command" if perm == "full" else "only these programs: " + ", ".join(allowed)
        specs.append(fn("run_command", f"Run a command ({which}). To feed text to a program, put it in stdin "
                        "(instead of echo ... | program). "
                        "Returns exit code and output.", {"command": s, "stdin": s}, ["command"]))
    return specs


class ToolRunner:
    def __init__(self, cwd: str, perm: str, allowed: list[str], env: dict, protect: str):
        self.cwd = Path(os.path.expanduser(cwd or "~"))
        self.perm, self.allowed, self.env = perm, allowed, env
        self.protect = Path(protect).resolve()

    def _path(self, p: str) -> Path:
        path = (self.cwd / os.path.expanduser(p)).resolve()
        if path == self.protect or self.protect in path.parents:
            raise PermissionError("AgentHub's data folder is off limits")
        return path

    def run(self, name: str, args: dict) -> str:
        try:
            out = getattr(self, "t_" + name)(**args)
        except AttributeError:
            out = f"error: unknown or unavailable tool {name}"
        except TypeError as exc:
            out = f"error: bad arguments for {name}: {exc}"
        except Exception as exc:  # report to the model; it can try something else
            out = f"error: {exc}"
        return out if len(out) <= MAX_OUT else out[:MAX_OUT] + f"\n…[truncated, {len(out)} chars]"

    def t_load_skill(self, name: str) -> str:
        sk = skills().get(name)
        if not sk:
            return "error: no such skill. Installed: " + ", ".join(sorted(skills()))
        return Path(sk["path"]).read_text("utf-8", errors="replace")

    def t_read_file(self, path: str, offset: int = 0, limit: int = 400) -> str:
        lines = self._path(path).read_text("utf-8", errors="replace").splitlines()
        return "\n".join(lines[offset:offset + limit]) + (
            f"\n…[{len(lines) - offset - limit} more lines]" if len(lines) > offset + limit else "")

    def t_list_dir(self, path: str = ".") -> str:
        p = self._path(path)
        return "\n".join(e.name + ("/" if e.is_dir() else "") for e in sorted(p.iterdir()))

    def t_write_file(self, path: str, content: str) -> str:
        if self.perm not in ("edit", "full"):
            return "error: your permissions do not allow writing files"
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")
        return f"wrote {len(content)} chars to {p}"

    def t_run_command(self, command: str, stdin: str = "") -> str:
        if self.perm == "full":
            argv = ["bash", "-lc", command]
        else:
            argv = shlex.split(command)
            if not argv or Path(argv[0]).name not in self.allowed:
                return "error: only these programs are allowed: " + (", ".join(self.allowed) or "none")
            if any(t in command for t in ("|", ";", "&&", "||", "`", "$(", ">", "<")):
                return ("error: pipes and redirects are not allowed at your permission level. Put the text in "
                        "the stdin argument instead, e.g. {\"command\": \"slop-chop fix\", \"stdin\": \"text to clean\"} "
                        "rather than echo \"text\" | slop-chop fix")
        proc = subprocess.run(argv, cwd=self.cwd, input=stdin or None, capture_output=True, text=True,
                              timeout=CMD_TIMEOUT, env=self.env)
        return f"exit {proc.returncode}\n{proc.stdout}" + (f"\n[stderr]\n{proc.stderr}" if proc.stderr else "")
