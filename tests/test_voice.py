import io
import json
import socket
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice


def fake_wyoming(reply: str):
    """One-shot Wyoming server: reads events until audio-stop, answers with a transcript."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = {"audio": b"", "types": []}

    def run():
        conn, _ = srv.accept()
        f = conn.makefile("rb")
        while True:
            ev = json.loads(f.readline())
            got["types"].append(ev["type"])
            if ev.get("payload_length"):
                got["audio"] += f.read(ev["payload_length"])
            if ev["type"] == "audio-stop":
                break
        data = json.dumps({"text": reply}).encode()
        conn.sendall(json.dumps({"type": "transcript", "data_length": len(data)}).encode() + b"\n" + data)
        conn.close()
        srv.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], got


class Voice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        voice.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_speakable_drops_code_links_and_markup(self):
        t = voice.speakable("Done **now**:\n```py\nx = 1\n```\nSee https://a.b/c and @bob [docs](http://d) [artifact:abc123]")
        self.assertEqual(t, "Done now : (code block) See (link) and bob docs (artifact)")

    def test_transcribe_streams_pcm_to_wyoming(self):
        port, got = fake_wyoming("hello there")
        voice.save({**voice.load(), "stt": {"wyoming": f"127.0.0.1:{port}", "language": "en"}})
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x01\x00" * 40000)   # 2.5 s
        self.assertEqual(voice.transcribe(buf.getvalue()), "hello there")
        self.assertEqual(got["types"][:2], ["transcribe", "audio-start"])
        self.assertEqual(got["types"][-1], "audio-stop")
        self.assertEqual(len(got["audio"]), 80000)

    def test_whisper_hallucinations_are_dropped_but_real_speech_kept(self):
        for junk in ("Thanks for watching!", "Thank you.", "you", "[BLANK_AUDIO]", "Please subscribe."):
            self.assertEqual(voice.clean_transcript(junk), "", junk)
        self.assertEqual(voice.clean_transcript("Thank you, now draw a cat."), "Thank you, now draw a cat.")

    def test_streaming_provider_passes_pcm_through_in_chunks(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = {}

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("X-Sample-Rate", "22050")
                self.end_headers()
                for _ in range(3):
                    self.wfile.write(b"\x01\x00" * 100)
                    self.wfile.flush()

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        voice.save({**voice.load(), "providers": [{"name": "T", "base_url": f"http://127.0.0.1:{srv.server_port}/v1", "stream": True}],
                    "default_provider": "T"})
        self.assertTrue(voice.streams(None))
        rate, chunks = voice.speak_stream("Hello there.", None, "Jessica")
        self.assertEqual(rate, 22050)
        self.assertEqual(len(b"".join(chunks)), 600)
        self.assertEqual((seen["stream"], seen["response_format"], seen["voice"]), (True, "pcm", "Jessica"))
        srv.server_close()

    def test_provider_falls_back_to_default(self):
        self.assertEqual(voice.provider("nope")["name"], "Kokoro")
        self.assertEqual(voice.provider("Chatterbox")["name"], "Chatterbox")


if __name__ == "__main__":
    unittest.main()
