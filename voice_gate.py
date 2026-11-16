"""Voice by name, no wake word: decide whether a transcribed sentence was spoken TO an agent.

  1. Stop words ("stop", "cancel", ...) while a voice conversation is open stop that agent.
  2. No agent named and no open conversation: ignored, the model is not asked.
  3. Otherwise the voice check model (Settings > Voice, a model from Settings > LLMs) decides
     "to" vs "about" for a named agent, or whether a nameless sentence is a follow-up.
  4. Model missing, failing or slow: ignored (fail closed).

A conversation stays open for WINDOW seconds after the owner's last accepted sentence, the
agent's last reply, or the end of its spoken playback. Kept in memory only. Rejected speech
is never stored or logged.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque

WINDOW = 30.0
STOP_RE = re.compile(r"^(stop|wait|cancel|never ?mind|hold on|shut up)[.!]*$", re.I)
PROMPT = (
    "You gate a voice assistant room. The microphone hears everything said in a room. Decide whether "
    "the CURRENT utterance is spoken TO one of the candidate agents (a request, question, command or "
    "answer meant for them), or not: talking ABOUT them to someone else, quoting, other people, TV, or "
    "unrelated talk. If a conversation with an agent is open (it spoke recently), a follow-up without its "
    'name can still be TO it. Reply with JSON only: {"to": "<handle>"} or {"to": null}.')

_lock = threading.Lock()
_conv: dict[str, dict] = {}   # channel -> {"agent", "until", "since", "turns": deque}


def reset():
    with _lock:
        _conv.clear()


def open_for(channel: str, now: float | None = None) -> str | None:
    now = time.time() if now is None else now
    with _lock:
        c = _conv.get(channel)
        if c and c["until"] > now:
            return c["agent"]
        _conv.pop(channel, None)
        return None


def saw_reply(channel: str, agent: str, text: str, ts: float):
    """An agent's reply in a channel: extends its open voice conversation (and gives follow-ups context)."""
    with _lock:
        c = _conv.get(channel)
        if c and c["agent"] == agent and ts >= c["since"]:
            c["turns"].append(("assistant", text[-300:]))
            c["until"] = max(c["until"], ts + WINDOW)


def played(channel: str, now: float | None = None):
    """The browser finished speaking a reply: the follow-up window starts now."""
    now = time.time() if now is None else now
    with _lock:
        c = _conv.get(channel)
        if c and c["until"] > now - WINDOW:
            c["until"] = max(c["until"], now + WINDOW)


def _accept(channel: str, agent: str, text: str, now: float):
    with _lock:
        c = _conv.get(channel)
        if not c or c["agent"] != agent:
            c = _conv[channel] = {"agent": agent, "since": now, "turns": deque(maxlen=6)}
        c["turns"].append(("user", text[-300:]))
        c["until"] = now + WINDOW


def _turns(channel: str) -> list[tuple[str, str]]:
    with _lock:
        c = _conv.get(channel)
        return list(c["turns"]) if c else []


def _parse(answer: str | None, candidates: list[str]) -> str | None | bool:
    """-> handle, None (no one), or False (unparseable)."""
    if not answer:
        return False
    answer = re.sub(r"<think>.*?(</think>|$)", "", answer, flags=re.S)
    m = re.search(r"\{.*\}", answer, re.S)
    try:
        to = json.loads(m.group(0)).get("to") if m else False
    except (ValueError, AttributeError):
        return False
    if to is None:
        return None
    if not isinstance(to, str):
        return False
    to = to.lstrip("@").lower()
    return to if to in candidates else None


def decide(text: str, channel: str, here: dict[str, str], ask, named, now: float | None = None) -> dict:
    """text: transcript. here: {handle: label} of agents in the channel. ask(prompt) -> model
    answer or None on failure. named(text, here) -> handles named in the text.
    -> {"accepted", "to", "reason", "open"}."""
    now = time.time() if now is None else now
    text = text.strip()
    current = open_for(channel, now)
    if current and current not in here:
        current = None

    def out(accepted, to, reason):
        return {"accepted": accepted, "to": to, "reason": reason, "open": open_for(channel, now)}
    if not here:
        return out(False, None, "no_agents")
    if current and STOP_RE.match(text):
        return out(False, current, "stopped")
    names = [n for n in named(text, here) if n in here]
    if not names and not current:
        return out(False, None, "no_name")
    candidates = names or [current]
    convo = "\n".join(f"{'owner' if r == 'user' else current}: {t}" for r, t in _turns(channel)) if current else ""
    who = "\n".join(f"@{n}" + (f" (name: {here[n]})" if here[n] and here[n] != n else "") for n in candidates)
    prompt = (f"Candidate agents:\n{who}\n\nOpen conversation: "
              + (f"with @{current}\n{convo or '(no turns yet)'}" if current else "none")
              + f"\n\nCURRENT utterance: {text}")
    to = _parse(ask(prompt), candidates)
    if to is False:
        return out(False, None, "decision_unavailable")
    if to is None:
        return out(False, None, "about" if names else "not_for_agent")
    _accept(channel, to, text, now)
    return out(True, to, "named" if to in names else "followup")
