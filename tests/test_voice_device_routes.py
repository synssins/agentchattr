import io
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import voice
import voice_devices
import voice_gate
from store import MessageStore


def wav(seconds=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class DeviceRoutes(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        voice_devices.data_dir = root
        voice_gate.reset()
        self.store = MessageStore(str(root / "log.jsonl"))
        self.dev, self.key = voice_devices.create("office-dot", "home")
        self.auth = {"Authorization": f"Bearer {self.key}"}
        registry = types.SimpleNamespace(get_active_names=lambda: ["margie"], get_instance=lambda n: {"label": "Margie"})
        api = FastAPI()
        for path, fn, methods in (("/api/device/listen", app.device_listen, ["POST"]),
                                  ("/api/device/reply", app.device_reply, ["GET"]),
                                  ("/api/device/audio/{message_id}", app.device_audio, ["GET"]),
                                  ("/api/device/played", app.device_played, ["POST"]),
                                  ("/voice-devices.txt", app.voice_devices_txt, ["GET"])):
            api.add_api_route(path, fn, methods=methods)
        self.transcripts = []
        self.answer = '{"to": "margie"}'
        for name, value in {"app": api, "store": self.store, "registry": registry, "agent_manager": None,
                            "room_settings": {"username": "alice"}}.items():
            self.stack.enter_context(patch.object(app, name, value))
        self.stack.enter_context(patch.object(app, "_voice_check_spec", lambda: {"name": "voice-check"}))
        self.asked = []
        self.stack.enter_context(patch.object(app, "_voice_ask", lambda spec, prompt: (self.asked.append(prompt), self.answer)[1]))
        self.stack.enter_context(patch.object(voice, "transcribe", lambda b: self.transcripts.pop(0)))
        self.stack.enter_context(patch.object(voice, "streams", lambda p: False))
        self.stack.enter_context(patch.object(voice, "speak", lambda text, *a: (b"RIFF....WAVEfake:" + text.encode(), "audio/wav")))
        app._install_security_middleware("session", {"server": {"port": 8787}})
        self.c = self.stack.enter_context(TestClient(api))

    def listen(self, said, **kw):
        self.transcripts.append(said)
        return self.c.post("/api/device/listen", content=wav(), headers={**self.auth, "Content-Type": "audio/wav"}, **kw)

    def test_listen_requires_device_key(self):
        self.transcripts.append("Margie, lights off.")
        r = self.c.post("/api/device/listen", content=wav(), headers={"Content-Type": "audio/wav"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.transcripts, ["Margie, lights off."])  # STT never ran
        for method, path in (("get", "/api/device/reply?after=1"), ("get", "/api/device/audio/1"),
                             ("post", "/api/device/played")):
            self.assertEqual(getattr(self.c, method)(path).status_code, 401, path)

    def test_listen_wav_named_is_posted_with_device_metadata(self):
        r = self.listen("Margie, turn off the office lights.").json()
        self.assertEqual((r["accepted"], r["to"], r["reason"]), (True, "margie", "named"))
        m = self.store.get_recent(1, channel="home")[-1]
        self.assertEqual(m["text"], "@margie Margie, turn off the office lights.")
        self.assertEqual(m["sender"], "alice")
        self.assertEqual(m["metadata"], {"voice": True, "device": "office-dot", "to": "margie"})

    def test_listen_text_body(self):
        r = self.c.post("/api/device/listen", json={"text": "Margie, what time is it?"}, headers=self.auth).json()
        self.assertTrue(r["accepted"])

    def test_listen_rejected_leaves_no_message(self):
        self.answer = '{"to": null}'
        r = self.listen("Margie is my assistant, she runs the lights.").json()
        self.assertEqual((r["accepted"], r["reason"]), (False, "about"))
        self.assertNotIn("text", r)
        self.assertEqual(self.store.get_recent(5, channel="home"), [])

    def test_listen_empty_or_noise_transcript_is_dropped(self):
        for said in ("[BLANK_AUDIO]", "(noise)", "", "  "):
            r = self.listen(said).json()
            self.assertEqual(r["reason"], "empty", said)
        self.assertEqual(self.asked, [])

    def test_listen_rejects_oversize_or_non_wav(self):
        big = self.c.post("/api/device/listen", content=wav(seconds=31), headers={**self.auth, "Content-Type": "audio/wav"})
        self.assertIn(big.status_code, (400, 413))
        junk = self.c.post("/api/device/listen", content=b"not a wav", headers={**self.auth, "Content-Type": "audio/wav"})
        self.assertEqual(junk.status_code, 400)

    def test_reply_long_poll_returns_agent_reply_with_speakable_text(self):
        mid = self.listen("Margie, find the KiCad site.").json()["message_id"]
        def later():
            time.sleep(0.3)
            self.store.add("margie", "Here: [KiCad](https://www.kicad.org/) is the site.", channel="home")
        threading.Thread(target=later).start()
        r = self.c.get(f"/api/device/reply?after={mid}&wait=5", headers=self.auth).json()
        self.assertIn("kicad.org", r["text"])
        self.assertNotIn("http", r["speak"])
        self.assertEqual(r["audio_url"], f"/api/device/audio/{r['message_id']}")
        audio = self.c.get(r["audio_url"], headers=self.auth)
        self.assertEqual(audio.status_code, 200)
        self.assertTrue(audio.content.startswith(b"RIFF"))
        self.assertNotIn(b"http", audio.content)  # speakable text was synthesised
        # the reply is now echo-protected on this device
        self.assertEqual(self.listen("Here is the site").json()["reason"], "echo")

    def test_reply_times_out_with_null_text(self):
        mid = self.listen("Margie, are you there?").json()["message_id"]
        t0 = time.time()
        r = self.c.get(f"/api/device/reply?after={mid}&wait=1", headers=self.auth).json()
        self.assertIsNone(r["text"])
        self.assertLess(time.time() - t0, 3)

    def test_reply_and_audio_only_for_this_devices_messages(self):
        other = self.store.add("alice", "@margie hi", channel="elsewhere")
        self.assertEqual(self.c.get(f"/api/device/reply?after={other['id']}&wait=1", headers=self.auth).status_code, 404)
        foreign = self.store.add("margie", "secret", channel="elsewhere")
        self.assertEqual(self.c.get(f"/api/device/audio/{foreign['id']}", headers=self.auth).status_code, 404)

    def test_played_extends_device_window(self):
        self.listen("Margie, read me the news.")
        real = time.time
        with patch.object(voice_gate.time, "time", lambda: real() + 25):
            self.c.post("/api/device/played", headers=self.auth)
        self.assertEqual(voice_gate.open_for("home", now=real() + 50, source="office-dot"), "margie")

    def test_protocol_doc_lists_routes(self):
        t = self.c.get("/voice-devices.txt").text
        for route in ("/api/device/listen", "/api/device/reply", "/api/device/audio/", "/api/device/played", "Bearer"):
            self.assertIn(route, t)


if __name__ == "__main__":
    unittest.main()
