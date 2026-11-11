import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_runner
from registry import RuntimeRegistry
from store import MessageStore


class Compaction(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=self.store,
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
        self.mgr = agent_runner.AgentManager(hub, tmp.name)
        self.mgr._apply = lambda name: None
        self.mgr.upsert({"name": "q", "kind": "api", "base_url": "http://x/v1", "context_messages": 3,
                         "cwd": tmp.name})
        self.calls = []

    def fake(self, spec, system, msgs, tools=None, tool_choice=None):
        self.calls.append((system, msgs))
        if system.startswith("You keep the running memory"):
            return {"content": "<think>hmm</think>MEMORY: alice wants a red bike"}
        return {"content": "ok"}

    def test_dropped_messages_fold_into_memory_and_return_next_turn(self):
        for i in range(10):
            self.store.add("alice", f"msg {i}")
        r = agent_runner.Runner(self.mgr, "q")
        with mock.patch.object(agent_runner, "call_api_message", self.fake), \
                mock.patch.object(agent_runner.threading, "Thread",
                                  lambda target, args, daemon: types.SimpleNamespace(start=lambda: target(*args))):
            r._api_turn("general")
            mem = r.memory("general")
            self.assertEqual(mem["text"], "MEMORY: alice wants a red bike")
            folded = self.calls[-1][1][0]["content"]
            self.assertIn("msg 0", folded)
            self.assertNotIn("msg 9", folded)          # still in the window, not folded
            r._api_turn("general")
        first = self.calls[-1][1][0]["content"]
        self.assertIn("MEMORY: alice wants a red bike", first)   # memory leads the next prompt
        self.assertEqual(len(self.calls), 3)                     # nothing new to fold the second time

    def test_compact_now_and_forget(self):
        for i in range(5):
            self.store.add("alice", f"msg {i}")
        r = agent_runner.Runner(self.mgr, "q")
        with mock.patch.object(agent_runner, "call_api_message", self.fake):
            self.assertIn("folded", r.compact_now("general", keep=2))
        self.assertIn("MEMORY", r.memory("general")["text"])
        self.assertEqual([m["text"] for m in r._window("general", 3)], ["msg 3", "msg 4"])
        r.forget("general")
        self.assertEqual(r.memory("general"), {})

    def test_old_tool_output_is_trimmed_past_the_budget(self):
        msgs = [{"role": "tool", "content": "x" * 1000} for _ in range(5)]
        agent_runner._trim_tool_output(msgs, budget=3000, keep=2)
        self.assertTrue(all("trimmed" in m["content"] for m in msgs[:3]))
        self.assertEqual(msgs[-1]["content"], "x" * 1000)


if __name__ == "__main__":
    unittest.main()
