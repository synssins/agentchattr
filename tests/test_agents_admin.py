"""Managed-agent CLI flags and agent self-registration (join, invites, triggers, revoke)."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import admin_api
import agent_runner
from registry import RuntimeRegistry
from store import MessageStore


class CliFlagsTest(unittest.TestCase):
    def cmd(self, kind, perm, session=None):
        return agent_runner.cli_command({"kind": kind, "permissions": perm}, "hi", session, "SYS")

    def test_claude_permission_levels(self):
        read = self.cmd("claude", "read")
        self.assertIn("--allowedTools", read)
        self.assertNotIn("--dangerously-skip-permissions", read)
        self.assertIn("acceptEdits", self.cmd("claude", "edit"))
        self.assertIn("--dangerously-skip-permissions", self.cmd("claude", "full"))
        self.assertIn("--resume", self.cmd("claude", "read", "abc"))

    def test_agy_permission_levels(self):
        self.assertIn("plan", self.cmd("agy", "read"))
        self.assertIn("accept-edits", self.cmd("agy", "edit"))
        self.assertIn("--dangerously-skip-permissions", self.cmd("agy", "full"))
        self.assertIn("--conversation", self.cmd("agy", "read", "c1"))

    def test_parse_cli_output(self):
        out = json.dumps({"result": " hello ", "session_id": "s1"})
        self.assertEqual(agent_runner.parse_cli_output(out), ("hello", "s1"))
        self.assertEqual(agent_runner.parse_cli_output("plain text"), ("plain text", None))


class JoinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        registry = RuntimeRegistry(data_dir=d)
        hub = types.SimpleNamespace(
            registry=registry, store=MessageStore(str(Path(d) / "log.jsonl")),
            room_settings={"username": "alice", "channels": ["general"]},
            _save_settings=lambda: None,
            _resolve_authenticated_agent=lambda req: registry.resolve_token(
                req.headers.get("authorization", "")[7:]))
        self.hub = hub
        admin_api.setup(hub, {"server": {"data_dir": d}})
        app = FastAPI()
        app.include_router(admin_api.router)
        self.c = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_join_poll_and_revoke(self):
        r = self.c.post("/api/agents/join", json={"name": "hermes", "description": "test"})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        self.assertEqual(self.c.post("/api/agents/join", json={"name": "hermes"}).status_code, 409)
        self.assertEqual(self.c.post("/api/agents/join", json={"name": "alice"}).status_code, 409)
        self.assertEqual(self.c.post("/api/agents/join", json={"name": "Bad Name"}).status_code, 400)

        # first message has id 0; it must still be delivered
        self.hub.store.add("alice", "@hermes hello", channel="general")
        (Path(self.tmp.name) / "hermes_queue.jsonl").write_text('{"channel": "general"}\n')
        trig = self.c.get("/api/triggers?wait=0", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertEqual(trig["triggers"][0]["messages"][0]["text"], "@hermes hello")
        self.assertEqual(self.c.get("/api/triggers?wait=0").status_code, 401)

        self.c.delete("/api/admin/remote-agents/hermes")
        r = self.c.get("/api/triggers?wait=0", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 401)  # revoked token is dead

    def test_invite_required_and_single_use(self):
        self.c.post("/api/admin/registration", json={"open_registration": False})
        self.assertEqual(self.c.post("/api/agents/join", json={"name": "a1"}).status_code, 403)
        code = self.c.post("/api/admin/invites", json={"label": "x"}).json()["code"]
        self.assertEqual(self.c.post("/api/agents/join", json={"name": "a1", "invite": code}).status_code, 200)
        self.assertEqual(self.c.post("/api/agents/join", json={"name": "a2", "invite": code}).status_code, 403)
        expired = self.c.post("/api/admin/invites", json={"ttl_hours": -1}).json()["code"]
        self.assertEqual(self.c.post("/api/agents/join", json={"name": "a3", "invite": expired}).status_code, 403)

    def test_v1_keys_hashed_and_revocable(self):
        key = self.c.post("/api/admin/v1-keys", json={"name": "opencode"}).json()["key"]
        self.assertEqual(admin_api.v1_key_name(key), "opencode")
        self.assertNotIn(key, (Path(self.tmp.name) / "v1_keys.json").read_text())
        self.c.delete("/api/admin/v1-keys/opencode")
        self.assertIsNone(admin_api.v1_key_name(key))


if __name__ == "__main__":
    unittest.main()


class AddressedTest(unittest.TestCase):
    def test_untagged_message_lets_agent_stay_quiet(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        m = types.SimpleNamespace(hub=types.SimpleNamespace(store=store), specs={"qwen": {}})
        r = agent_runner.Runner(m, "qwen")
        store.add("alice", "Who broke the build?")
        self.assertFalse(r._addressed("general"))
        store.add("alice", "@qwen you did")
        self.assertTrue(r._addressed("general"))
        store.add("alice", "@all status?")
        self.assertFalse(r._addressed("general"))  # may stay quiet if not its work
        store.add("alice", "@qwen3 not you")
        self.assertFalse(r._addressed("general"))


class ChannelMembershipTest(unittest.TestCase):
    def test_in_channel_and_duplicate_key(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name))
        mgr = agent_runner.AgentManager(hub, tmp.name)
        base = {"kind": "api", "base_url": "http://x/v1", "enabled": False}
        self.assertIsNone(mgr.upsert(dict(base, name="a", api_key="k1")))
        self.assertIsNone(mgr.upsert(dict(base, name="b", channels=["irc-work"], copy_key_from="a")))
        self.assertTrue(mgr.in_channel("a", "general"))           # everyone is in #general
        self.assertFalse(mgr.in_channel("a", "irc-work"))         # other channels: only added agents
        self.assertTrue(mgr.in_channel("b", "irc-work"))
        self.assertTrue(mgr.in_channel("b", "general"))
        self.assertFalse(mgr.in_channel("remote-x", "irc-work"))
        mgr.set_member("remote-x", "irc-work", True)              # any agent can be added
        self.assertEqual(mgr.channels_of("remote-x"), ["general", "irc-work"])
        self.assertNotIn("channels", mgr.specs["b"])              # membership is stored separately
        self.assertEqual(mgr.specs["b"]["api_key"], "k1")          # duplicate reused the stored key
        self.assertNotIn("copy_key_from", mgr.specs["b"])


class SpawnTest(unittest.TestCase):
    def test_spawn_pins_channel_fresh_context_and_despawn(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=store)
        mgr = agent_runner.AgentManager(hub, tmp.name)
        mgr._apply = lambda name: None  # don't start runner threads in this test
        self.assertIsNone(mgr.upsert({"name": "qwen", "kind": "api", "base_url": "http://x/v1", "api_key": "k"}))
        store.add("alice", "old history")
        note = mgr.spawn("@qwen", "irc-work")
        self.assertIn("Spawned @qwen-2", note)
        child = mgr.specs["qwen-2"]
        self.assertEqual((child["api_key"], child["spawned"]), ("k", True))
        self.assertEqual(mgr.channels_of("qwen-2"), ["general", "irc-work"])
        self.assertEqual(child["context_from"], 1)  # history before the spawn is not its context
        self.assertIn("Spawned @qwen-3", mgr.spawn("qwen", "irc-work"))
        self.assertIn("No agent 'nope'", mgr.spawn("nope", "general"))
        self.assertIn("wasn't created with /spawn", mgr.despawn("qwen"))
        self.assertEqual(mgr.despawn("qwen-2"), "Removed @qwen-2.")
        self.assertNotIn("qwen-2", mgr.specs)
        self.assertEqual(mgr.channels_of("qwen-2"), ["general"])  # membership cleaned up
