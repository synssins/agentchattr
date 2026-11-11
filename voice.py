"""Voice: text-to-speech through OpenAI-style /audio/speech providers (Kokoro, Chatterbox,
external APIs) and speech-to-text through a Wyoming server (wyoming-whisper).

Providers live in data/voice.json (owner-editable in Settings > Voice). Each agent picks a
provider, voice and speed on its own page (spec fields voice_provider, voice, voice_speed).
Cloning uploads a short sample to a provider that keeps a voice library (Chatterbox POST /voices).
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import threading
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

data_dir = Path("./data")
_lock = threading.Lock()

DEFAULTS = {
    "providers": [
        {"name": "Kokoro", "base_url": "http://127.0.0.1:8880/v1", "model": "kokoro", "format": "mp3",
         "voices_url": "http://127.0.0.1:8880/v1/audio/voices", "default_voice": "af_bella"},
        {"name": "Chatterbox", "base_url": "http://127.0.0.1:4123", "model": "", "format": "wav",
         "voices_url": "http://127.0.0.1:4123/voices", "clone_url": "http://127.0.0.1:4123/voices",
         "default_voice": "Nora"},
    ],
    "default_provider": "Kokoro",
    "stt": {"wyoming": "127.0.0.1:10300", "language": "en"},
}
TIMEOUT = 120  # Chatterbox on CPU takes ~9 s per sentence


def setup(cfg: dict):
    global data_dir
    data_dir = Path(cfg.get("server", {}).get("data_dir", "./data"))


def load() -> dict:
    try:
        return {**DEFAULTS, **json.loads((data_dir / "voice.json").read_text("utf-8"))}
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULTS))


def save(conf: dict):
    with _lock:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = data_dir / "voice.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # may hold API keys
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(conf, f, indent=2)
        os.replace(tmp, data_dir / "voice.json")


def provider(name: str | None) -> dict:
    conf = load()
    ps = conf["providers"]
    return (next((p for p in ps if p["name"] == name), None)
            or next((p for p in ps if p["name"] == conf.get("default_provider")), None) or ps[0])


def _get(url: str, key: str = "", timeout: int = 8):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def voices() -> dict:
    """{provider name: [voice names]} (an unreachable provider lists none, with an error)."""
    out = {}
    for p in load()["providers"]:
        try:
            raw = _get(p["voices_url"], p.get("api_key", "")) if p.get("voices_url") else {"voices": p.get("voices", [])}
            items = raw.get("voices", raw) if isinstance(raw, dict) else raw
            names = [v if isinstance(v, str) else v.get("name") or v.get("id") for v in items]
            out[p["name"]] = {"voices": sorted(n for n in names if n), "clone": bool(p.get("clone_url"))}
        except Exception as exc:
            out[p["name"]] = {"voices": [], "clone": bool(p.get("clone_url")), "error": str(exc)[:160]}
    return out


CODE_RE = re.compile(r"```.*?```", re.S)


def speakable(text: str) -> str:
    """Chat text -> what to read aloud: no code, artifacts, attachments, links or markdown."""
    t = CODE_RE.sub(" (code block) ", text or "")
    t = re.sub(r"\[artifact:[^\]]+\]", " (artifact) ", t)
    t = re.sub(r"\[attach(?:ed)?[^\]]*\]", " ", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)          # [label](url) -> label
    t = re.sub(r"https?://\S+", " (link) ", t)
    t = re.sub(r"(^|\s)@([\w-]+)", r"\1\2", t)                 # @name -> name
    t = re.sub(r"[*_#>|~]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def speak(text: str, provider_name: str | None = None, voice: str | None = None,
          speed: float | None = None) -> tuple[bytes, str]:
    """-> (audio bytes, media type)."""
    p = provider(provider_name)
    text = speakable(text)[:3000]
    if not text:
        raise ValueError("nothing to read aloud")
    body = {"input": text, "voice": voice or p.get("default_voice") or "alloy",
            "response_format": p.get("format") or "mp3"}
    if p.get("model"):
        body["model"] = p["model"]
    if speed:
        body["speed"] = max(0.5, min(2.0, float(speed)))
    headers = {"Content-Type": "application/json"}
    if p.get("api_key"):
        headers["Authorization"] = f"Bearer {p['api_key']}"
    url = p["base_url"].rstrip("/") + "/audio/speech"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read(), r.headers.get("Content-Type") or "audio/mpeg"


def speak_stream(text: str, provider_name: str | None = None, voice: str | None = None,
                 speed: float | None = None):
    """For providers with "stream": true -> (sample rate, iterator of raw PCM16 mono bytes).
    Bytes are passed on as the provider produces them, so playback starts on the first chunk."""
    p = provider(provider_name)
    text = speakable(text)[:3000]
    if not text:
        raise ValueError("nothing to read aloud")
    body = {"input": text, "voice": voice or p.get("default_voice") or "alloy",
            "response_format": "pcm", "stream": True}
    if p.get("model"):
        body["model"] = p["model"]
    if speed:
        body["speed"] = max(0.5, min(2.0, float(speed)))
    headers = {"Content-Type": "application/json"}
    if p.get("api_key"):
        headers["Authorization"] = f"Bearer {p['api_key']}"
    req = urllib.request.Request(p["base_url"].rstrip("/") + "/audio/speech", data=json.dumps(body).encode(),
                                 headers=headers)
    resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    rate = int(resp.headers.get("X-Sample-Rate") or p.get("sample_rate") or 24000)

    def chunks():
        with resp:
            while True:
                b = resp.read1(8192)
                if not b:
                    return
                yield b
    return rate, chunks()


def streams(provider_name: str | None) -> bool:
    return bool(provider(provider_name).get("stream"))


def clone(provider_name: str, name: str, filename: str, sample: bytes) -> str:
    """Upload a voice sample to a provider's voice library -> the new voice name."""
    p = provider(provider_name)
    if not p.get("clone_url"):
        raise ValueError(f"{p['name']} has no voice library to clone into")
    name = re.sub(r"[^\w -]", "", name).strip()[:40]
    if not name:
        raise ValueError("a voice name is required")
    boundary = uuid.uuid4().hex
    parts = []
    for field, value in (("voice_name", name), ("language", load()["stt"].get("language", "en"))):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
    safe = re.sub(r"[^\w.-]", "_", filename or "sample.wav")
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="voice_file"; filename="{safe}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n'.encode() + sample + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(p["clone_url"], data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60):
            pass
    except urllib.error.HTTPError as exc:
        raise ValueError(f"{p['name']} refused the sample: {exc.read()[:200].decode('utf-8', 'replace')}")
    return name


# ---------------------------------------------------------------- speech-to-text (Wyoming)

# Whisper invents these on silence or noise (trained on video subtitles); drop them when they are
# the whole transcript.
HALLUCINATIONS = re.compile(
    r"^(thanks? (you )?(so much )?for watching|thank you( very much)?|thanks|bye|you|"
    r"please subscribe|(like and )?subscribe( to (my|the) channel)?|subtitles by.*|"
    r"see you (next time|in the next video)|\.+|\[.*\]|\(.*\))[.!]*$", re.I)


def clean_transcript(text: str) -> str:
    t = (text or "").strip()
    return "" if HALLUCINATIONS.match(t) else t


def _send(sock, etype: str, data: dict | None = None, payload: bytes = b""):
    head = {"type": etype, "data": data or {}}
    if payload:
        head["payload_length"] = len(payload)
    sock.sendall(json.dumps(head).encode() + b"\n" + payload)


def _read_event(f) -> dict:
    line = f.readline()
    if not line:
        raise ConnectionError("speech-to-text server closed the connection")
    ev = json.loads(line)
    if ev.get("data_length"):
        ev["data"] = {**(ev.get("data") or {}), **json.loads(f.read(ev["data_length"]))}
    if ev.get("payload_length"):
        f.read(ev["payload_length"])
    return ev


def transcribe(wav_bytes: bytes) -> str:
    """WAV (any rate; 16-bit) -> text, through the Wyoming server in voice.json stt.wyoming."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        pcm = w.readframes(w.getnframes())
    stt = load()["stt"]
    host, port = stt["wyoming"].rsplit(":", 1)
    fmt = {"rate": rate, "width": width, "channels": channels}
    with socket.create_connection((host, int(port)), timeout=60) as sock:
        _send(sock, "transcribe", {"language": stt.get("language") or "en"})
        _send(sock, "audio-start", fmt)
        step = rate * width * channels  # one second per chunk
        for i in range(0, len(pcm), step):
            _send(sock, "audio-chunk", fmt, pcm[i:i + step])
        _send(sock, "audio-stop")
        f = sock.makefile("rb")
        while True:
            ev = _read_event(f)
            if ev["type"] == "transcript":
                return clean_transcript((ev.get("data") or {}).get("text", ""))
            if ev["type"] == "error":
                raise RuntimeError((ev.get("data") or {}).get("text", "speech-to-text failed"))
