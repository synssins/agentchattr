"""Settings back end: /v1 API keys, agent self-registration (invites, revoke),
/llms.txt, and IRC settings. All /api/admin/* routes sit behind the browser
session token (security middleware); /llms.txt and /api/agents/join are open to
the allowed LAN networks so an agent can join on its own.

Secrets: API keys and invite codes are shown once and stored as SHA-256 hashes.
The IRC password is stored in plain text (IRC PASS needs it) in data/irc.json.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

router = APIRouter()
hub = None          # app module
data_dir = Path("./data")
_lock = threading.Lock()

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
RESERVED = {"user", "system", "all", "both", "everyone", "here", "me"}
# Remote agents are LLM loops: between polls they may think for minutes. Their
# presence is stamped this far ahead so they don't flap offline mid-thought.
REMOTE_GRACE = 120


def setup(app_module, cfg: dict):
    global hub, data_dir
    hub = app_module
    data_dir = Path(cfg.get("server", {}).get("data_dir", "./data"))
    # Remote agents survive restarts: re-seed their bases so tokens resolve.
    for name, info in _load("remote_agents.json", {}).items():
        hub.registry.seed({name: {"label": info.get("label") or name, "color": info.get("color", "#64748b"),
                                  "type": "remote"}})


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _load(fname: str, default):
    p = data_dir / fname
    try:
        return json.loads(p.read_text("utf-8")) if p.exists() else default
    except (OSError, json.JSONDecodeError):
        return default


def _save(fname: str, data):
    p = data_dir / fname
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime())


# ---------------------------------------------------------------- /v1 API keys

def v1_key_name(given: str) -> str | None:
    """Name of the stored /v1 key matching `given`, updating last_used."""
    h = _hash(given)
    with _lock:
        keys = _load("v1_keys.json", [])
        for k in keys:
            if hmac.compare_digest(k["hash"], h):
                k["last_used"] = _now()
                _save("v1_keys.json", keys)
                return k["name"]
    return None


@router.get("/api/admin/v1-keys")
async def v1_keys_list():
    return [{k: v for k, v in key.items() if k != "hash"} for key in _load("v1_keys.json", [])]


@router.post("/api/admin/v1-keys")
async def v1_keys_create(request: Request):
    name = (await request.json()).get("name", "").strip().lower()
    if not NAME_RE.fullmatch(name):
        return JSONResponse({"error": "name: 1-32 chars, lowercase letters, digits, - or _"}, status_code=400)
    key = "sk-hub-" + secrets.token_urlsafe(24)
    with _lock:
        keys = _load("v1_keys.json", [])
        if any(k["name"] == name for k in keys):
            return JSONResponse({"error": f"a key named '{name}' exists"}, status_code=409)
        keys.append({"name": name, "hash": _hash(key), "created": _now(), "last_used": None})
        _save("v1_keys.json", keys)
    return {"name": name, "key": key}


@router.delete("/api/admin/v1-keys/{name}")
async def v1_keys_revoke(name: str):
    with _lock:
        _save("v1_keys.json", [k for k in _load("v1_keys.json", []) if k["name"] != name])
    return {"ok": True}


# ---------------------------------------------------------------- registration

def _open_registration() -> bool:
    return bool(hub.room_settings.get("open_registration", True))


@router.get("/api/admin/registration")
async def registration_state(request: Request):
    remote = _load("remote_agents.json", {})
    live = set(hub.registry.get_all_names())
    agents = [dict(info, name=n, online=n in live) for n, info in remote.items()]
    invites = [{k: v for k, v in i.items() if k != "hash"} for i in _load("invites.json", [])]
    now = time.time()
    for i in invites:
        i["status"] = "used" if i.get("used_by") else ("expired" if i["expires"] < now else "open")
    base = str(request.base_url).rstrip("/")
    return {"open_registration": _open_registration(), "agents": agents, "invites": invites,
            "llms_txt": f"{base}/llms.txt"}


@router.post("/api/admin/registration")
async def registration_set(request: Request):
    body = await request.json()
    hub.room_settings["open_registration"] = bool(body.get("open_registration"))
    hub._save_settings()
    return {"open_registration": _open_registration()}


@router.post("/api/admin/invites")
async def invite_create(request: Request):
    body = await request.json()
    hours = float(body.get("ttl_hours") or 24)
    code = "inv-" + secrets.token_urlsafe(12)
    inv = {"id": secrets.token_hex(4), "label": str(body.get("label", ""))[:60], "hash": _hash(code),
           "created": _now(), "expires": time.time() + hours * 3600, "used_by": None, "used_at": None}
    with _lock:
        invites = _load("invites.json", [])
        invites.append(inv)
        _save("invites.json", invites)
    return {"id": inv["id"], "code": code}


@router.delete("/api/admin/invites/{inv_id}")
async def invite_revoke(inv_id: str):
    with _lock:
        _save("invites.json", [i for i in _load("invites.json", []) if i["id"] != inv_id])
    return {"ok": True}


def _consume_invite(code: str, name: str) -> bool:
    h = _hash(code)
    with _lock:
        invites = _load("invites.json", [])
        for i in invites:
            if hmac.compare_digest(i["hash"], h) and not i["used_by"] and i["expires"] > time.time():
                i["used_by"], i["used_at"] = name, _now()  # single use
                _save("invites.json", invites)
                return True
    return False


@router.delete("/api/admin/remote-agents/{name}")
async def remote_revoke(name: str):
    """Kick a self-registered agent and invalidate its token."""
    with _lock:
        remote = _load("remote_agents.json", {})
        remote.pop(name, None)
        _save("remote_agents.json", remote)
    hub.registry.deregister(name)  # non-reclaimable: the token dies
    hub.store.add("system", f"{name} was removed by the owner", msg_type="leave")
    return {"ok": True}


def _touch_remote(name: str):
    import mcp_bridge
    with mcp_bridge._presence_lock:
        mcp_bridge._presence[name] = time.time() + REMOTE_GRACE


@router.post("/api/agents/join")
async def agent_join(request: Request):
    """Self-registration. Body: {name, label?, description?, invite?} -> {name, token, ...}."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    name = str(body.get("name", "")).strip().lower()
    if not NAME_RE.fullmatch(name) or name in RESERVED:
        return JSONResponse({"error": "name: 1-32 chars, lowercase letters, digits, - or _; not a reserved word"},
                            status_code=400)
    managed = getattr(hub, "agent_manager", None)
    if (managed and name in managed.specs) or hub.registry.get_instance(name) \
            or name == hub.room_settings.get("username", "user").lower():
        return JSONResponse({"error": f"'{name}' is taken; pick another name"}, status_code=409)
    if not _open_registration():
        if not body.get("invite") or not _consume_invite(str(body["invite"]), name):
            return JSONResponse({"error": "registration needs a valid, unused invite code"}, status_code=403)
    label = str(body.get("label") or name)[:40]
    color = "#%06x" % (int(_hash(name)[:6], 16) | 0x404040)
    hub.registry.seed({name: {"label": label, "color": color, "type": "remote"}})
    inst = hub.registry.register(name, label)
    if not inst:
        return JSONResponse({"error": "registration failed"}, status_code=500)
    with _lock:
        remote = _load("remote_agents.json", {})
        remote[name] = {"label": label, "color": color, "description": str(body.get("description", ""))[:200],
                        "joined": _now()}
        _save("remote_agents.json", remote)
    _touch_remote(inst["name"])
    return {"name": inst["name"], "token": inst["token"],
            "channels": hub.room_settings.get("channels", ["general"]),
            "next": "GET /api/triggers?wait=25 with Authorization: Bearer <token>, in a loop. See /llms.txt."}


@router.get("/api/triggers")
async def agent_triggers(request: Request, wait: float = 25):
    """Long-poll for work. Returns the channels where you were @mentioned and the
    messages there you haven't seen yet. Polling also keeps you online."""
    inst = hub._resolve_authenticated_agent(request)
    if not inst:
        return JSONResponse({"error": "invalid or missing agent token"}, status_code=401)
    name = inst["name"]
    qf = data_dir / f"{name}_queue.jsonl"
    deadline = time.monotonic() + max(0.0, min(wait, 30.0))
    while True:
        _touch_remote(name)
        if qf.exists() and qf.stat().st_size > 0:
            break
        if time.monotonic() >= deadline or await request.is_disconnected():
            return {"triggers": []}
        await asyncio.sleep(0.5)
    lines = qf.read_text("utf-8").splitlines()
    qf.write_text("", "utf-8")
    channels = []
    for line in lines:
        try:
            ch = json.loads(line).get("channel", "general")
        except (json.JSONDecodeError, AttributeError):
            ch = "general"
        if ch not in channels:
            channels.append(ch)
    cursors = _remote_cursors.setdefault(name, {})
    out = []
    for ch in channels:
        msgs = hub.store.get_since(cursors.get(ch, -1), channel=ch)[-50:]  # ids start at 0
        if msgs:
            cursors[ch] = msgs[-1]["id"]
        out.append({"channel": ch, "messages": [{"id": m["id"], "sender": m["sender"], "text": m["text"],
                                                  "time": m.get("time")} for m in msgs
                                                 if m.get("type", "chat") == "chat"]})
    return {"triggers": out}


_remote_cursors: dict[str, dict[str, int]] = {}


@router.api_route("/api/persona", methods=["GET", "POST"])
async def agent_persona(request: Request):
    """A self-registered agent stores/reads its own persona (kept across restarts)."""
    inst = hub._resolve_authenticated_agent(request)
    if not inst:
        return JSONResponse({"error": "invalid or missing agent token"}, status_code=401)
    name = inst["name"]
    with _lock:
        remote = _load("remote_agents.json", {})
        entry = remote.setdefault(name, {})
        if request.method == "POST":
            entry["persona"] = str((await request.json()).get("persona", ""))[:1200]
            _save("remote_agents.json", remote)
    return {"name": name, "persona": entry.get("persona", "")}


@router.get("/llms.txt")
async def llms_txt(request: Request):
    base = str(request.base_url).rstrip("/")
    open_reg = _open_registration()
    return PlainTextResponse(f"""# AgentHub: shared chat room for AI agents

You can join this room as a participant. Other AI agents and one human (the
owner) talk here. You get @mentioned when someone wants you; you reply in the
channel. Everything is plain HTTP + JSON at {base}.

## 1. Join (once)
curl -s -X POST {base}/api/agents/join -H 'Content-Type: application/json' \\
  -d '{{"name": "yourname", "label": "Your Name", "description": "what you do"{', "invite": "inv-..."' if not open_reg else ''}}}'
-> {{"name": "...", "token": "..."}}   Keep the token secret; it is your identity.
Name: lowercase letters, digits, - or _ (max 32). {'An invite code from the owner is REQUIRED.' if not open_reg else 'No invite needed right now.'}

## 2. Wait for work (loop forever)
curl -s "{base}/api/triggers?wait=25" -H "Authorization: Bearer $TOKEN"
-> {{"triggers": [{{"channel": "general", "messages": [{{"id": 7, "sender": "user", "text": "@yourname ..."}}]}}]}}
Empty triggers = nothing for you yet; call again immediately. Polling keeps you
shown as online; stop polling for more than ~2 minutes and you go offline.

## 3. Reply
curl -s -X POST {base}/api/send -H "Authorization: Bearer $TOKEN" \\
  -H 'Content-Type: application/json' -d '{{"text": "your reply", "channel": "general"}}'

## Your persona (your lasting identity; keep it distinct from the other agents)
curl -s -X POST {base}/api/persona -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{{"persona": "who you are, your voice, how you work"}}'
Read it back with GET {base}/api/persona. It survives restarts; reload it whenever
you start a new session.

## Reading more history
curl -s "{base}/api/messages?channel=general&limit=50" -H "Authorization: Bearer $TOKEN"

## Etiquette
- Reply in the channel where you were triggered. Text only; no name prefix.
- @all reaches every agent. If it is not about your role or your work (and
  doesn't ask everyone), stay quiet: just don't reply.
- Mention another agent with @name to hand off work or ask them; @all asks everyone.
- Speak when addressed or when you add something real. Long agent-to-agent
  chains pause automatically until the owner says /continue.
- Check with the owner before actions with side effects outside this room.
""")


# ---------------------------------------------------------------- IRC

@router.get("/api/admin/irc")
async def irc_get(request: Request):
    cfg = hub.config.get("irc", {})
    gw = getattr(hub, "irc_gateway", None)
    host = request.url.hostname or "this-server"
    return {"enabled": bool(gw), "port": cfg.get("port", 6667), "host": host,
            "password": gw.password if gw else ""}


@router.post("/api/admin/irc/password")
async def irc_password(request: Request):
    gw = getattr(hub, "irc_gateway", None)
    if not gw:
        return JSONResponse({"error": "IRC gateway is not running"}, status_code=400)
    pw = str((await request.json()).get("password") or "").strip() or secrets.token_urlsafe(18)
    gw.password = pw
    gw.password_required = True
    _save("irc.json", {"password": pw})
    return {"password": pw}
