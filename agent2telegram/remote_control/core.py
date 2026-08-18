"""Remote Control core — state, durable event spool and the Claude Code hook adapter.

This module is the **hot path**. Claude Code runs it once per hook event (including once per
``MessageDisplay`` text delta), so it must start fast and finish fast:

  * standard library only, and only cheap imports;
  * **no relative imports** — it can be executed directly as a script
    (``python3 -S -E .../agent2telegram/remote_control/core.py``), which skips ``site``
    processing and roughly halves interpreter start-up;
  * no network I/O, no subprocesses, no transcript scanning, no locks;
  * fail open — any internal error exits 0 so a broken mirror can never break Claude Code.

Everything it does is: read one JSON payload from stdin, stat/read two tiny local files, and
(when the session is mirrored) rename one small JSON file into a spool directory. The
long-running Agent2Telegram bridge picks the spool up and does all Telegram work.

State layout (under ``$AGENT2TELEGRAM_STATE``, default ``~/.local/state/agent2telegram``)::

    remote-control/
    ├── sessions/<claude-session-id>.json      # fast index: which bridge a session is bound to
    └── <bridge-slug>/                         # bridge slug = sanitized tmux session name
        ├── enabled/<claude-session-id>        # authoritative "remote control is on" marker
        ├── origin/<claude-session-id>.json    # terminal | telegram, per session
        ├── events/<ordered-event-id>.json     # durable spool, consumed by the bridge
        └── consumer_heartbeat                 # bridge liveness, used to bound the spool

Directories are 0700 and files 0600: event payloads can contain source code or anything else
the model printed, so they are private to the user and deleted as soon as they are forwarded.
"""
from __future__ import annotations

import json
import os
import sys
import time

# --------------------------------------------------------------------------- constants

#: Spool format version — the consumer ignores events it does not understand.
EVENT_VERSION = 1

#: Origin prefixes that always mean "this prompt came from Telegram", on top of the bridge's
#: configured ``origin_prefix``. Kept in sync with ``AttachBridge._origins``.
DEFAULT_ORIGINS = ("Telegram:", "[TG]")

#: How long the bridge's heartbeat may be stale before we consider it gone. A restart takes a
#: second or two; this is generous enough that a queued turn survives one, and short enough
#: that a machine whose bridge is switched off does not accumulate events forever.
CONSUMER_GRACE = 600.0

#: Hard cap on the spool while the consumer is down. Once reached we stop writing rather than
#: fill the disk — a mirror is best-effort, the local session is not.
MAX_PENDING = 500

#: Hook events we translate into mirror events. Anything else is ignored.
HOOK_EVENTS = (
    "SessionStart", "SessionEnd", "UserPromptSubmit", "MessageDisplay",
    "PreToolUse", "PostToolUseFailure", "PermissionRequest", "Notification",
    "SubagentStart", "SubagentStop", "TaskCreated", "TaskCompleted",
    "Stop", "StopFailure",
)

#: SessionStart sources that intentionally start with Remote Control OFF. ``clear`` and
#: ``compact`` keep it, because /clear and /compact are not "a new session" to the user.
RESET_SOURCES = ("startup", "resume", "fork")

#: SessionEnd reasons that are NOT a real exit — /clear and session switches emit SessionEnd
#: immediately followed by SessionStart, and tearing state down there would disconnect a user
#: who only typed /clear.
NON_TERMINAL_END_REASONS = ("clear", "resume")

#: Notification types worth a remote message. ``permission_prompt`` is deliberately absent:
#: PermissionRequest already covers it and we do not want the same event twice.
ACTIONABLE_NOTIFICATIONS = ("idle_prompt", "agent_needs_input", "agent_completed")


# --------------------------------------------------------------------------- paths

def state_dir() -> str:
    """Agent2Telegram's state directory (same env var the rest of the package honours)."""
    return os.path.expanduser(
        os.environ.get("AGENT2TELEGRAM_STATE")
        or os.path.join("~", ".local", "state", "agent2telegram")
    )


def root_dir() -> str:
    return os.path.join(state_dir(), "remote-control")


def sessions_dir() -> str:
    return os.path.join(root_dir(), "sessions")


def slug(name: str) -> str:
    """Filesystem-safe id for a bridge (its tmux session) or a Claude session id."""
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in (name or "")) or "bridge"


def session_index(session_id: str) -> str:
    return os.path.join(sessions_dir(), slug(session_id) + ".json")


def bridge_dir(bridge: str) -> str:
    return os.path.join(root_dir(), slug(bridge))


def enabled_marker(bridge: str, session_id: str) -> str:
    return os.path.join(bridge_dir(bridge), "enabled", slug(session_id))


def origin_path(bridge: str, session_id: str) -> str:
    return os.path.join(bridge_dir(bridge), "origin", slug(session_id) + ".json")


def events_dir(bridge: str) -> str:
    return os.path.join(bridge_dir(bridge), "events")


def heartbeat_path(bridge: str) -> str:
    return os.path.join(bridge_dir(bridge), "consumer_heartbeat")


def _mkdir_private(path: str) -> None:
    """Create *path* and tighten every level of it up to the Remote Control root to 0700.

    ``os.makedirs(mode=...)`` only applies the mode to the LEAF; intermediate directories get
    the default 0777 & ~umask. Since these directories hold message content, walk back up and
    fix each one."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    root = root_dir()
    current = path
    while True:
        try:
            os.chmod(current, 0o700)
        except OSError:
            pass
        if current == root or len(current) <= len(root):
            return
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def _write_private(path: str, text: str) -> None:
    """Atomic 0600 write: create a temp file in the same directory, then rename over."""
    _mkdir_private(os.path.dirname(path))
    tmp = f"{path}.{os.getpid()}.{os.urandom(3).hex()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except BaseException:
        _unlink(tmp)
        raise
    os.replace(tmp, path)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- binding / enable

def bind_session(session_id: str, *, bridge: str, config_path: str = "",
                 origins=(), label: str = "") -> None:
    """Turn Remote Control ON for one Claude session.

    The binding file is the hook's fast path: one ``open()`` tells it whether this session is
    mirrored at all and, if so, which bridge owns it — no tmux call, no config parsing.
    """
    prefixes = [p for p in dict.fromkeys(tuple(origins) + DEFAULT_ORIGINS) if p]
    _write_private(enabled_marker(bridge, session_id), "enabled\n")
    _write_private(session_index(session_id), json.dumps({
        "bridge": slug(bridge),
        "config": config_path,
        "origins": prefixes,
        "label": label,
        "since": time.time(),
    }, ensure_ascii=False))


def unbind_session(session_id: str, bridge: str = "") -> bool:
    """Turn Remote Control OFF for one Claude session. Returns True if it had been on."""
    binding = session_binding(session_id)
    bridge = bridge or (binding or {}).get("bridge", "")
    was_on = bool(binding) or (bool(bridge) and os.path.exists(enabled_marker(bridge, session_id)))
    if bridge:
        _unlink(enabled_marker(bridge, session_id))
        _unlink(origin_path(bridge, session_id))
    _unlink(session_index(session_id))
    return was_on


def session_binding(session_id: str):
    """The binding for *session_id*, or None when Remote Control is off for it (hot path)."""
    if not session_id:
        return None
    data = _read_json(session_index(session_id))
    if not isinstance(data, dict) or not data.get("bridge"):
        return None
    # The per-bridge marker is authoritative; a stale index without it means "off".
    if not os.path.exists(enabled_marker(data["bridge"], session_id)):
        return None
    return data


def is_enabled(session_id: str) -> bool:
    return session_binding(session_id) is not None


# --------------------------------------------------------------------------- origin

def set_origin(bridge: str, session_id: str, origin: str, prompt_id: str = "") -> None:
    _write_private(origin_path(bridge, session_id), json.dumps(
        {"origin": origin, "prompt_id": prompt_id, "ts": time.time()}, ensure_ascii=False))


def get_origin(bridge: str, session_id: str) -> str:
    """Origin of the current turn. Defaults to ``terminal``: a session only becomes
    Telegram-driven once a prefixed prompt actually arrives."""
    data = _read_json(origin_path(bridge, session_id))
    if isinstance(data, dict) and data.get("origin") == "telegram":
        return "telegram"
    return "terminal"


def classify_origin(user_input: str, origins=()) -> str:
    """``telegram`` when the prompt carries a Telegram routing prefix, else ``terminal``."""
    text = (user_input or "").lstrip()
    for prefix in tuple(origins) + DEFAULT_ORIGINS:
        if prefix and text.startswith(prefix):
            return "telegram"
    return "terminal"


# --------------------------------------------------------------------------- spool

def event_id() -> str:
    """Ordered, collision-resistant spool id: nanosecond clock + pid + randomness."""
    return f"{time.time_ns():020d}-{os.getpid():07d}-{os.urandom(4).hex()}"


def consumer_alive(bridge: str, grace: float = CONSUMER_GRACE) -> bool:
    try:
        return (time.time() - os.stat(heartbeat_path(bridge)).st_mtime) <= grace
    except OSError:
        return False       # no heartbeat yet → treat as down and rely on the spool cap


def pending_count(bridge: str) -> int:
    try:
        return sum(1 for n in os.listdir(events_dir(bridge)) if n.endswith(".json"))
    except OSError:
        return 0


def write_event(bridge: str, event: dict) -> str:
    """Spool one event durably. Returns its id, or "" when it was dropped.

    Dropping only happens while the bridge is not consuming *and* the spool already holds
    :data:`MAX_PENDING` events — an unattended machine must not fill its disk with a mirror
    nobody is reading.
    """
    if not consumer_alive(bridge) and pending_count(bridge) >= MAX_PENDING:
        return ""
    eid = event_id()
    event = dict(event)
    event.setdefault("v", EVENT_VERSION)
    event.setdefault("ts", time.time())
    event["id"] = eid
    _write_private(os.path.join(events_dir(bridge), eid + ".json"),
                   json.dumps(event, ensure_ascii=False))
    return eid


def read_events(bridge: str, limit: int = 400):
    """Pending events oldest-first as ``(path, payload_or_None)``. ``None`` means corrupt."""
    d = events_dir(bridge)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return []
    out = []
    for name in names[:limit]:
        path = os.path.join(d, name)
        data = _read_json(path)
        out.append((path, data if isinstance(data, dict) else None))
    return out


def ack_event(path: str) -> None:
    """Remove a processed event — payloads are never retained after forwarding."""
    _unlink(path)


def touch_heartbeat(bridge: str) -> None:
    try:
        _mkdir_private(bridge_dir(bridge))
        path = heartbeat_path(bridge)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        os.chmod(path, 0o600)          # the mode above only applies when the file is created
    except OSError:
        pass


# --------------------------------------------------------------------------- summaries

#: Credential shapes we mask before a summary can reach a chat app. Compiled lazily: ``re``
#: costs a couple of milliseconds to import and the hottest event (MessageDisplay) never
#: summarizes anything, so the hot path must not pay for it.
_SECRET_PATTERN = r"""(?xi)
    (?:
        (?:sk|rk|pk)-[A-Za-z0-9_-]{16,}          # OpenAI-style keys
      | gh[pousr]_[A-Za-z0-9]{16,}               # GitHub tokens
      | xox[abposr]-[A-Za-z0-9-]{10,}            # Slack tokens
      | AKIA[0-9A-Z]{12,}                        # AWS access key ids
      | eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}   # JWTs
      | \b\d{6,12}:[A-Za-z0-9_-]{30,}\b          # Telegram bot tokens
      | (?<=[Bb]earer\s)[A-Za-z0-9._-]{12,}
      | (?<==)[A-Za-z0-9+/_-]{24,}={0,2}         # FOO=<long opaque value>
    )
    """

_SECRET_ASSIGN_PATTERN = r"""(?xi)
    \b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY|ACCESS_KEY|PRIVATE_KEY)
       [A-Za-z0-9_]*)
    \s*[:=]\s*
    (?:"[^"]*"|\'[^\']*\'|\S+)
    """

_SECRET_RES = None


def _secret_res():
    global _SECRET_RES
    if _SECRET_RES is None:
        import re
        _SECRET_RES = (re.compile(_SECRET_ASSIGN_PATTERN), re.compile(_SECRET_PATTERN))
    return _SECRET_RES


def redact(text: str) -> str:
    """Mask anything that looks like a credential before it can reach a chat app."""
    assign_re, secret_re = _secret_res()
    text = assign_re.sub(lambda m: f"{m.group(1)}=***", str(text))
    return secret_re.sub("***", text)


def short(s, n: int = 58) -> str:
    """One-line, markdown-free, length-capped version of *s* (shared with the readers)."""
    s = " ".join(str(s).split()).replace("**", "").replace("`", "")
    return s if len(s) <= n else s[:n - 1] + "…"


def tool_summary(name: str, inp) -> str:
    """One-line, emoji-prefixed summary of a Claude Code tool call.

    Only a handful of known-safe fields are ever read, and the result is redacted — a tool
    input can contain credentials the model was handed, and it must never be dumped verbatim.
    """
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return "🛠️ " + short(redact(inp.get("description") or inp.get("command", "command")))
    if name == "Read":
        return "📄 Reading " + short(os.path.basename(inp.get("file_path", "")) or "file")
    if name in ("Edit", "Write", "NotebookEdit"):
        return "✏️ Editing " + short(os.path.basename(inp.get("file_path", "")) or "file")
    if name in ("Grep", "Glob"):
        return "🔎 Searching " + short(redact(inp.get("pattern", "")))
    if name == "WebFetch":
        url = inp.get("url", "")
        host = url
        try:
            import urllib.parse
            host = urllib.parse.urlparse(url).netloc or url
        except Exception:
            pass
        return "🌐 Web " + short(host)
    if name == "WebSearch":
        return "🔎 Web search: " + short(redact(inp.get("query", "")))
    if name in ("Agent", "Task"):
        return "🤖 " + short(redact(inp.get("description") or "subagent"))
    if name.startswith("mcp__"):
        return "🔌 " + short(name.replace("mcp__", "").replace("__", " "))
    return "🛠️ " + short(name or "tool")


def subagent_summary(agent_type: str) -> str:
    return "🤖 " + short(agent_type or "subagent") + " running"


# --------------------------------------------------------------------------- hook adapter

def _emit(bridge: str, session_id: str, kind: str, **fields) -> str:
    return write_event(bridge, {"type": kind, "session_id": session_id,
                                "bridge": slug(bridge), **fields})


def _handle_session_start(payload: dict) -> None:
    """Fresh startup / resume / fork begin with Remote Control OFF — the same rule native
    Remote Control uses. /clear and /compact deliberately keep it on."""
    source = payload.get("source", "")
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    if source in RESET_SOURCES:
        unbind_session(session_id)
        return
    binding = session_binding(session_id)
    if binding and source in ("clear", "compact"):
        # The turn is over and the transcript was rewritten; start the next turn clean.
        set_origin(binding["bridge"], session_id, "terminal")


def _handle_session_end(payload: dict, binding: dict) -> None:
    reason = payload.get("reason", "")
    if reason in NON_TERMINAL_END_REASONS:
        return                                   # /clear and session switches are not an exit
    bridge, session_id = binding["bridge"], payload.get("session_id", "")
    _emit(bridge, session_id, "session_end", reason=reason)
    unbind_session(session_id, bridge)


def handle(payload: dict) -> int:
    """Translate one Claude Code hook payload into (at most) one spooled mirror event."""
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id", "")

    # SessionStart runs before/independently of any binding — it is what clears one.
    if event == "SessionStart":
        _handle_session_start(payload)
        return 0

    binding = session_binding(session_id)
    if binding is None:
        return 0                                  # not mirrored → the common case, ~0 work
    bridge = binding["bridge"]

    if event == "SessionEnd":
        _handle_session_end(payload, binding)
        return 0

    if event == "UserPromptSubmit":
        text = payload.get("user_input", "") or ""
        origin = classify_origin(text, binding.get("origins") or ())
        set_origin(bridge, session_id, origin, payload.get("prompt_id", ""))
        if origin == "terminal":
            _emit(bridge, session_id, "prompt", text=text)
        return 0

    # Everything below mirrors the LOCAL seat only. Telegram-originated turns are already
    # forwarded by the attach bridge; mirroring them here would double every message.
    if get_origin(bridge, session_id) != "terminal":
        return 0

    if event == "MessageDisplay":
        _emit(bridge, session_id, "message",
              message_id=payload.get("message_id", ""),
              turn_id=payload.get("turn_id", ""),
              index=payload.get("index", 0),
              delta=payload.get("delta", "") or "",
              final=bool(payload.get("final")))
    elif event == "PreToolUse":
        _emit(bridge, session_id, "tool",
              summary=tool_summary(payload.get("tool_name", ""), payload.get("tool_input")),
              tool_use_id=payload.get("tool_use_id", ""))
    elif event == "PostToolUseFailure":
        _emit(bridge, session_id, "tool_failed",
              summary=tool_summary(payload.get("tool_name", ""), payload.get("tool_input")),
              error=short(redact(payload.get("error", "")), 160))
    elif event == "PermissionRequest":
        _emit(bridge, session_id, "permission",
              summary=tool_summary(payload.get("tool_name", ""), payload.get("tool_input")),
              tool_name=short(payload.get("tool_name", ""), 40))
    elif event == "Notification":
        kind = payload.get("notification_type", "")
        if kind in ACTIONABLE_NOTIFICATIONS:
            _emit(bridge, session_id, "notification", notification_type=kind)
    elif event == "SubagentStart":
        _emit(bridge, session_id, "subagent_start",
              agent_type=short(payload.get("agent_type", ""), 40))
    elif event == "SubagentStop":
        _emit(bridge, session_id, "subagent_stop",
              agent_type=short(payload.get("agent_type", ""), 40))
    elif event == "TaskCreated":
        _emit(bridge, session_id, "task_created",
              task_name=short(redact(payload.get("task_name", "")), 80))
    elif event == "TaskCompleted":
        _emit(bridge, session_id, "task_completed",
              task_name=short(redact(payload.get("task_name", "")), 80))
    elif event == "Stop":
        _emit(bridge, session_id, "turn_end",
              last_assistant_message=payload.get("last_assistant_message", "") or "")
    elif event == "StopFailure":
        _emit(bridge, session_id, "turn_failed",
              error_type=short(payload.get("error_type", "unknown"), 40),
              error_message=short(redact(payload.get("error_message", "")), 400))
    return 0


def hook_main(stdin=None) -> int:
    """Entry point for the registered hook command. Always exits 0 (fail open)."""
    try:
        payload = json.load(stdin if stdin is not None else sys.stdin)
        if isinstance(payload, dict):
            handle(payload)
    except Exception:
        pass                                      # a broken mirror never breaks Claude Code
    return 0


if __name__ == "__main__":                        # executed directly: python3 -S -E core.py
    sys.exit(hook_main())
