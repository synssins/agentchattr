"""Managed-agent CLI flags and agent self-registration (join, invites, triggers, revoke)."""

import json
import os
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
        full = self.cmd("agy", "full")
        perms = json.loads(full[full.index("--settings") + 1])["permissions"]
        self.assertEqual(perms["deny"], [])                      # overrides agy's global denies
        self.assertIn("command(*)", perms["allow"])
        edit = self.cmd("agy", "edit")
        self.assertIn("command(*)", json.loads(edit[edit.index("--settings") + 1])["permissions"]["deny"])
        self.assertNotIn("--settings", self.cmd("agy", "read"))

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
        admin_api._remote_cursors.clear()
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


class PersonaTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=self.store,
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
        self.mgr = agent_runner.AgentManager(hub, tmp.name)
        self.mgr._apply = lambda name: None
        self.mgr.upsert({"name": "qwen", "kind": "api", "base_url": "http://x/v1", "persona": "Grumpy QA."})

    def test_agent_sets_persona_from_reply(self):
        r = agent_runner.Runner(self.mgr, "qwen")
        r._api_turn = lambda ch: "Fine.\n[persona]Dry-witted reviewer who blames the build.[/persona]"
        r._turn("general")
        self.assertEqual(self.mgr.specs["qwen"]["persona"], "Dry-witted reviewer who blames the build.")
        posted = [m["text"] for m in self.store.get_recent(5) if m["sender"] == "qwen"]
        self.assertEqual(posted, ["Fine."])  # block stripped from the posted message

    def test_persona_in_prompt_kept_on_fresh_not_copied_on_spawn(self):
        self.store.add("alice", "old")
        self.assertIn("Grumpy QA.", agent_runner.Runner(self.mgr, "qwen")._system())
        self.mgr.fresh("qwen")
        self.assertEqual(self.mgr.specs["qwen"]["persona"], "Grumpy QA.")
        self.assertEqual(self.mgr.specs["qwen"]["context_from"], 1)
        self.mgr.spawn("qwen", "dev")
        self.assertNotIn("persona", self.mgr.specs["qwen-2"])
        self.assertIn("no persona yet", agent_runner.Runner(self.mgr, "qwen-2")._system())


class TurnTakingTest(unittest.TestCase):
    def test_turns_in_a_channel_are_sequential_and_see_each_other(self):
        import threading as th
        import time as tm
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=store,
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
        mgr = agent_runner.AgentManager(hub, tmp.name)
        mgr._apply = lambda name: None
        for n in ("a", "b"):
            mgr.upsert({"name": n, "kind": "api", "base_url": "http://x/v1"})
        seen = {}

        def make(name):
            r = agent_runner.Runner(mgr, name)
            def api_turn(ch):
                seen[name] = [m["text"] for m in store.get_recent(10, channel=ch)]
                tm.sleep(0.2)
                return f"I am {name}"
            r._api_turn = api_turn
            return r
        store.add("alice", "@all names please")
        threads = [th.Thread(target=make(n)._turn, args=("general",)) for n in ("a", "b")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        first, second = sorted(seen, key=lambda n: len(seen[n]))
        self.assertIn(f"I am {first}", seen[second])  # the second saw the first's reply


class RenameTest(unittest.TestCase):
    def test_agent_renames_itself_everywhere(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=store,
                                    room_settings={"username": "alice"},
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
        mgr = agent_runner.AgentManager(hub, tmp.name)
        mgr.upsert({"name": "agy-2", "kind": "api", "base_url": "http://x/v1", "persona": "P",
                    "channels": ["sonorium"]})
        mgr.upsert({"name": "claudecode-2", "kind": "api", "base_url": "http://x/v1"})
        try:
            r = mgr.runners["agy-2"]
            r._api_turn = lambda ch: "Call me Marcus.\n[rename]Marcus[/rename]"
            r._turn("sonorium")
            self.assertIn("marcus", mgr.specs)
            self.assertNotIn("agy-2", mgr.specs)
            self.assertEqual((mgr.specs["marcus"]["label"], mgr.specs["marcus"]["persona"]), ("Marcus", "P"))
            self.assertTrue(mgr.in_channel("marcus", "sonorium"))
            self.assertTrue(hub.registry.get_instance("marcus"))
            self.assertIsNone(hub.registry.get_instance("agy-2"))
            posted = [m for m in store.get_recent(5) if m["type"] == "chat"]
            self.assertEqual((posted[-1]["sender"], posted[-1]["text"]), ("marcus", "Call me Marcus."))
            self.assertIn("taken", mgr.rename("claudecode-2", "marcus"))   # no duplicates
            self.assertIn("taken", mgr.rename("claudecode-2", "alice"))
            mgr.upsert({"name": "agy", "kind": "api", "base_url": "http://x/v1", "enabled": False})
            self.assertIn("Spawned @agy-3", mgr.spawn("agy", "general"))     # agy-2 family still has marcus
            self.assertTrue(hub.registry.get_instance("marcus"))
        finally:
            for x in list(mgr.runners.values()):
                x.stop.set()


class ChannelDeleteTest(unittest.TestCase):
    def test_delete_removes_only_channel_bound_spawns(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=store)
        mgr = agent_runner.AgentManager(hub, tmp.name)
        mgr._apply = lambda name: None
        mgr.upsert({"name": "agy", "kind": "api", "base_url": "http://x/v1"})
        mgr.spawn("agy", "sonorium")                      # agy-2: only in #sonorium
        mgr.spawn("agy", "sonorium")                      # agy-3: also added to #other
        mgr.set_member("agy-3", "other", True)
        mgr.set_member("agy", "sonorium", True)           # original, added manually
        mgr.set_member("remote-x", "sonorium", True)      # self-registered
        mgr.state["agy"] = {"sessions": {"sonorium": "s1", "general": "s0"}, "cursors": {}}
        self.assertEqual(mgr.channel_deleted("sonorium"), ["agy-2"])
        self.assertEqual(sorted(mgr.specs), ["agy", "agy-3"])
        self.assertEqual(mgr.channels_of("agy-3"), ["general", "other"])
        self.assertEqual(mgr.channels_of("agy"), ["general"])
        self.assertEqual(mgr.state["agy"]["sessions"], {"general": "s0"})
        mgr.channel_renamed("other", "renamed")
        self.assertEqual(mgr.channels_of("agy-3"), ["general", "renamed"])

    def test_shutdown_kills_inflight_cli_process(self):
        import subprocess as sp
        r = agent_runner.Runner(types.SimpleNamespace(hub=types.SimpleNamespace(
            registry=types.SimpleNamespace(deregister=lambda n: None))), "x")
        r.proc = sp.Popen(["sleep", "30"])
        r.shutdown()
        self.assertIsNotNone(r.proc.wait(timeout=5))


class AttachmentTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.updir = Path(tmp.name) / "uploads"
        self.updir.mkdir()
        (self.updir / "abc.png").write_bytes(b"\x89PNG fake")
        self.store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        self.store.upload_dir = self.updir
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=self.store,
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
        self.mgr = agent_runner.AgentManager(hub, tmp.name)
        self.mgr._apply = lambda name: None
        self.mgr.upsert({"name": "qwen", "kind": "api", "base_url": "http://x/v1"})
        self.store.add("alice", "@qwen what is this?", attachments=[
            {"name": "image.png", "url": "/uploads/abc.png", "kind": "image", "media_type": "image/png"}])
        self.r = agent_runner.Runner(self.mgr, "qwen")

    def test_text_has_absolute_path(self):
        text = self.r._text(self.store.get_recent(1)[0])
        self.assertIn(f"[attached image image.png: {self.updir.resolve() / 'abc.png'}]", text)

    def test_api_agent_gets_image_inline_then_text_only_fallback(self):
        import urllib.error
        calls = []

        def fake(spec, system, msgs, tools=None):
            calls.append([dict(m) for m in msgs])
            if len(calls) == 1:
                raise urllib.error.HTTPError("u", 400, "no vision", {}, None)
            return {"content": "ok"}
        orig = agent_runner.call_api_message
        agent_runner.call_api_message = fake
        try:
            self.assertEqual(self.r._api_turn("general"), "ok")
        finally:
            agent_runner.call_api_message = orig
        parts = calls[0][-1]["content"]
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIsInstance(calls[1][-1]["content"], str)          # retry without the image
        self.assertIn("abc.png", calls[1][-1]["content"])           # but still with its path

    def test_anthropic_conversion(self):
        out = agent_runner._to_anthropic([{"type": "text", "text": "hi"},
                                          {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}])
        self.assertEqual(out[1], {"type": "image", "source": {"type": "base64", "data": "QUJD", "media_type": "image/png"}})


class SecretsTest(JoinTest):
    def test_secret_only_reaches_granted_agent_and_follows_rename(self):
        r = self.c.post("/api/admin/secrets", json={"name": "GitHub", "env": "gh_token", "value": "ghp_x",
                                                     "grants": ["claude"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("ghp_x", r.text)                                  # never returned
        self.assertEqual(admin_api.secrets_for("claude"), {"GH_TOKEN": "ghp_x"})
        self.assertEqual(admin_api.secrets_for("agy"), {})
        self.c.post("/api/admin/secrets", json={"name": "GitHub", "env": "GH_TOKEN", "grants": ["claude", "agy"]})
        self.assertEqual(admin_api.secrets_for("agy"), {"GH_TOKEN": "ghp_x"})  # blank value kept
        admin_api.rename_grants("claude", "marcus")
        self.assertEqual(admin_api.secrets_for("marcus"), {"GH_TOKEN": "ghp_x"})
        self.assertEqual(admin_api.secrets_for("claude"), {})
        admin_api.rename_grants("agy", None)
        self.assertEqual(admin_api.secrets_for("agy"), {})

    def test_claude_file_tools_blocked_from_data_dir(self):
        cmd = agent_runner.cli_command({"kind": "claude", "permissions": "full"}, "hi", None, "S",
                                       protect="/data/agenthub/data")
        self.assertIn("Read(/data/agenthub/data/**)", cmd)


class ToolLoopTest(unittest.TestCase):
    def setUp(self):
        import agent_tools
        self.at = agent_tools
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / "work").mkdir()
        (self.tmp / "work" / "notes.txt").write_text("line1\nline2\n")
        (self.tmp / "data").mkdir()
        (self.tmp / "data" / "secrets.json").write_text("SECRET")

    def runner(self, perm, allowed=()):
        return self.at.ToolRunner(str(self.tmp / "work"), perm, list(allowed), dict(os.environ),
                                  protect=str(self.tmp / "data"))

    def test_permission_levels(self):
        names = lambda perm, allowed=(): [t["function"]["name"] for t in self.at.tool_specs(perm, list(allowed))]
        self.assertEqual(names("read"), ["load_skill", "read_file", "list_dir"])
        self.assertIn("write_file", names("edit"))
        self.assertNotIn("run_command", names("edit"))
        self.assertIn("run_command", names("read", ["slop-chop"]))
        self.assertIn("run_command", names("full"))

    def test_tools_behave(self):
        r = self.runner("read", ["echo"])
        self.assertEqual(r.run("read_file", {"path": "notes.txt"}), "line1\nline2")
        self.assertIn("off limits", r.run("read_file", {"path": "../data/secrets.json"}))
        self.assertIn("not allow", r.run("write_file", {"path": "x", "content": "y"}))
        self.assertIn("only these programs", r.run("run_command", {"command": "ls"}))
        self.assertIn("pipes and redirects are not allowed", r.run("run_command", {"command": "echo hi | cat"}))
        self.assertEqual(r.run("run_command", {"command": "echo hi"}), "exit 0\nhi\n")
        full = self.runner("full")
        self.assertEqual(full.run("run_command", {"command": "echo a | tr a b"}), "exit 0\nb\n")
        self.assertIn("wrote", self.runner("edit").run("write_file", {"path": "out.txt", "content": "z"}))

    def test_loop_runs_tool_then_answers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=store,
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
        mgr = agent_runner.AgentManager(hub, tmp.name)
        mgr._apply = lambda name: None
        mgr.upsert({"name": "qwen", "kind": "api", "base_url": "http://x/v1", "permissions": "read",
                    "allowed_commands": ["echo"], "cwd": str(self.tmp / "work")})
        store.add("alice", "@qwen run echo")
        seen = []

        def fake(spec, system, msgs, tools=None):
            seen.append(msgs[-1])
            if len(seen) == 1:
                self.assertIn("slop-chop", system) if "slop-chop" in self.at.skills() else None
                return {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "run_command", "arguments": json.dumps({"command": "echo tool-ran"})}}]}
            return {"content": "done: " + msgs[-1]["content"].split("\n")[1]}
        orig = agent_runner.call_api_message
        agent_runner.call_api_message = fake
        try:
            self.assertEqual(agent_runner.Runner(mgr, "qwen")._api_turn("general"), "done: tool-ran")
        finally:
            agent_runner.call_api_message = orig
        self.assertEqual(seen[1]["role"], "tool")

    def test_claude_allowed_commands_become_bash_rules(self):
        cmd = agent_runner.cli_command({"kind": "claude", "permissions": "read", "allowed_commands": ["slop-chop"]},
                                       "hi", None, "S")
        self.assertIn("Bash(slop-chop:*)", cmd)


class McpTest(JoinTest):
    def add_server(self, grants):
        return self.c.post("/api/admin/mcp", json={
            "name": "echo", "transport": "stdio", "command": sys.executable,
            "args": [str(Path(__file__).parent / "mcp_echo_server.py")], "env": "TOKEN=s3cret",
            "grants": grants})

    def test_crud_masks_values_and_test_lists_tools(self):
        r = self.add_server(["qwen"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("s3cret", r.text)
        self.assertEqual(r.json()[0]["env_names"], ["TOKEN"])
        self.c.post("/api/admin/mcp", json={"name": "echo", "transport": "stdio", "command": sys.executable,
                                            "args": [str(Path(__file__).parent / "mcp_echo_server.py")],
                                            "env": "TOKEN=", "grants": ["qwen"]})
        import mcp_hub
        self.assertEqual(mcp_hub.servers()[0]["env"], {"TOKEN": "s3cret"})  # blank value kept
        self.assertEqual(self.c.post("/api/admin/mcp/echo/test").json(), {"tools": ["shout"]})
        bad = self.c.post("/api/admin/mcp", json={"name": "bad", "transport": "http", "url": "http://127.0.0.1:9/mcp"})
        self.assertEqual(bad.status_code, 200)
        self.assertEqual(self.c.post("/api/admin/mcp/bad/test").status_code, 502)

    def test_api_agent_and_claude_get_granted_servers_only(self):
        import mcp_hub
        self.add_server(["qwen"])
        specs, names = mcp_hub.tool_specs_for("qwen")
        self.assertEqual([s["function"]["name"] for s in specs], ["mcp__echo__shout"])
        self.assertEqual(mcp_hub.call_tool(names["mcp__echo__shout"][0], "shout", {"text": "hi"}), "HI")
        self.assertEqual(mcp_hub.tool_specs_for("other")[0], [])
        path, rules = mcp_hub.claude_config("qwen", self.tmp.name)
        self.assertEqual(rules, ["mcp__echo"])
        self.assertIn("echo", json.loads(Path(path).read_text())["mcpServers"])
        self.assertEqual(oct(Path(path).stat().st_mode & 0o777), "0o600")
        cmd = agent_runner.cli_command({"kind": "claude", "permissions": "read"}, "hi", None, "S", mcp=(path, rules))
        self.assertIn("--mcp-config", cmd)
        self.assertIn("mcp__echo", cmd)
        admin_api.rename_grants("qwen", "marcus")
        self.assertEqual(len(mcp_hub.tool_specs_for("marcus")[0]), 1)


class RulebooksTest(unittest.TestCase):
    def setUp(self):
        import rulebooks
        self.rb = rulebooks
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        rulebooks.setup({"server": {"data_dir": str(self.tmp / "data")}}, self.tmp)

    def test_starters_mirrored_and_prompt_sections(self):
        books = self.rb.load()
        self.assertEqual([b["name"] for b in books], ["Writing (general)", "Story writing", "Code & processes"])
        self.assertTrue((self.tmp / "rulebooks" / "code-processes.md").exists())
        books[1]["channels"] = ["fiction"]
        books[2]["agents"] = ["lazlo"]
        self.rb.save(books)
        p = self.rb.prompt_for("lazlo", "general")
        self.assertIn("Bespoke is bad", p)                       # attached to the agent: in full
        self.assertIn("story-writing.md", p)                       # others: listed with a path
        self.assertNotIn("Show through action", p)
        self.assertIn("Show through action", self.rb.prompt_for("kestrel", "fiction"))  # channel attach
        self.rb.rename_agent("lazlo", "marcus")
        self.assertIn("Bespoke is bad", self.rb.prompt_for("marcus", "general"))
        msg = self.rb.review_message(self.rb.find("story writing"), ["kestrel"], "Once upon a time.")
        self.assertTrue(msg.startswith("@kestrel Please review"))
        self.assertIn("Once upon a time.", msg)


class ToolBudgetTest(unittest.TestCase):
    def test_out_of_steps_asks_for_summary(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=store,
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
        mgr = agent_runner.AgentManager(hub, tmp.name)
        mgr._apply = lambda name: None
        mgr.upsert({"name": "q", "kind": "api", "base_url": "http://x/v1", "max_tool_steps": 2, "cwd": tmp.name})
        store.add("alice", "@q work")
        calls = []

        def fake(spec, system, msgs, tools=None):
            calls.append(tools)
            if tools:
                return {"content": "", "tool_calls": [{"id": str(len(calls)), "type": "function",
                        "function": {"name": "list_dir", "arguments": "{}"}}]}
            return {"content": "Did X, Y left."}
        orig = agent_runner.call_api_message
        agent_runner.call_api_message = fake
        try:
            self.assertEqual(agent_runner.Runner(mgr, "q")._api_turn("general"), "Did X, Y left.")
        finally:
            agent_runner.call_api_message = orig
        self.assertEqual(len(calls), 3)          # 2 tool rounds, then one summary call
        self.assertIsNone(calls[-1])             # summary call has no tools
