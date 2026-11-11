"""Live agent activity: Claude Code stream-json steps and early posting while background tasks run."""

import json
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_runner
from store import MessageStore

EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "s9"},
    {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "plan it"},
                                                  {"type": "tool_use", "id": "t1", "name": "Bash",
                                                   "input": {"command": "ls"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Real answer."}]}},
    {"type": "result", "result": "Real answer.", "session_id": "s9"},
]
LATE = [{"type": "result", "result": "(no reply)", "session_id": "s9"}]


class ClaudeStreamTest(unittest.TestCase):
    def runner(self):
        self.store = MessageStore(str(Path(tempfile.mkdtemp()) / "log.jsonl"))
        mgr = types.SimpleNamespace(hub=types.SimpleNamespace(store=self.store), specs={"c": {"kind": "claude"}},
                                    set_inflight=lambda *a: None)
        r = agent_runner.Runner(mgr, "c")
        r._act_reset()
        return r

    def proc(self, pause: float):
        script = textwrap.dedent(f"""
            import json, sys, time
            for e in {EVENTS!r}: print(json.dumps(e), flush=True)
            time.sleep({pause})
            for e in {LATE!r}: print(json.dumps(e), flush=True)
        """)
        return subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def test_steps_recorded_and_tool_output_attached(self):
        r = self.runner()
        r.proc = self.proc(0)
        (text, session), _ = r._claude_stream("general")
        self.assertEqual((text, session), ("Real answer.", "s9"))   # "(no reply)" wake-up keeps the answer
        kinds = [(s["k"], s.get("name"), s.get("out")) for s in r.activity]
        self.assertEqual(kinds, [("thinking", None, None), ("tool", "Bash", "a.txt")])  # reply not a step
        self.assertEqual(self.store.get_recent(5), [])              # posting is left to _turn

    def test_reply_posted_while_background_tasks_run(self):
        r = self.runner()
        r.proc = self.proc(2.5)
        (text, _), _ = r._claude_stream("general")
        self.assertEqual(text, "(no reply)")                         # already posted; not posted twice
        posted = self.store.get_recent(5)
        self.assertEqual([m["text"] for m in posted], ["Real answer."])
        steps = posted[0]["metadata"]["activity"]["steps"]
        self.assertEqual([s["k"] for s in steps], ["thinking", "tool"])
        self.assertEqual(r.activity[0]["k"], "status")               # a fresh activity for the wait


if __name__ == "__main__":
    unittest.main()
