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
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "Notification",
    "SubagentStart", "SubagentStop", "TaskCreated", "TaskCompleted",
    "PreCompact", "PostCompact", "Elicitation", "ElicitationResult",
    "Stop", "StopFailure",
)

#: SessionStart sources that intentionally start with Remote Control OFF. ``clear`` and
#: ``compact`` keep it, because /clear and /compact are not "a new session" to the user.
RESET_SOURCES = ("startup", "resume", "fork")

#: SessionEnd reasons that are NOT a real exit — /clear and session switches emit SessionEnd
#: immediately followed by SessionStart, and tearing state down there would disconnect a user
#: who only typed /clear.
NON_TERMINAL_END_REASONS = ("clear", "resume")

#: Tools that BLOCK the session on a human answer instead of running. Claude Code emits no
#: turn-end for them, so without special handling the chat shows a session that looks busy
#: forever while it is really waiting at the keyboard.
BLOCKING_TOOLS = ("AskUserQuestion",)

#: How much transcript the connect-time recap may read, and how much of it to keep. This is
#: the ONLY place this project reads a transcript, and it is a one-off on an explicit user
#: action — the live mirror is driven entirely by hooks.
#: Generous on purpose: a real session's tail is mostly tool_use and tool_result records with
#: no text at all, so a small window yields one lonely turn instead of a usable digest. Read
#: once, on connect, so a megabyte costs nothing.
RECAP_TAIL_BYTES = 1024 * 1024
RECAP_MESSAGES = 6
RECAP_CHARS = 400

#: Notification types worth a remote message. ``permission_prompt`` is deliberately absent:
#: PermissionRequest already covers it and we do not want the same event twice.
ACTIONABLE_NOTIFICATIONS = ("idle_prompt", "agent_needs_input", "agent_completed")

#: How long a PermissionRequest hook waits for a remote Allow/Deny before giving up and letting
#: the normal terminal prompt appear. This is the ONE place the hook deliberately blocks: Claude
#: Code is already stopped waiting for a human, so waiting costs nothing that was not already
#: being spent — but it must stay short enough that someone sitting at the keyboard is not
#: locked out of their own prompt for long.
PERMISSION_TIMEOUT = 90.0
#: The same wait, for a question Claude asks with its own picker. Longer than a permission
#: because reading several options and choosing is a slower decision than allow/deny — but it is
#: still a lockout for anyone sitting at the keyboard, so it stays bounded and configurable.
QUESTION_TIMEOUT = 120.0
#: Poll interval while waiting for the decision file (a few hundred stats over the whole wait).
PERMISSION_POLL = 0.1
#: An answered-but-never-collected decision (the hook died, Claude Code was killed) is swept
#: after this long so the directory cannot accumulate.
DECISION_TTL = 3600.0


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


def decisions_dir(bridge: str) -> str:
    return os.path.join(bridge_dir(bridge), "decisions")


def decision_path(bridge: str, request_id: str) -> str:
    return os.path.join(decisions_dir(bridge), slug(request_id) + ".json")


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
                 origins=(), label: str = "", permissions: bool = True,
                 permission_timeout: float = PERMISSION_TIMEOUT,
                 question_timeout: float = QUESTION_TIMEOUT, cwd: str = "",
                 quiet: bool = True) -> None:
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
        "cwd": cwd,
        "permissions": bool(permissions),
        "permission_timeout": float(permission_timeout),
        "question_timeout": float(question_timeout),
        # Progress arrives without a sound; only a decision or the end of a turn notifies.
        "quiet": bool(quiet),
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


def sessions_for_bridge(bridge: str) -> dict:
    """Every session currently mirrored through *bridge*, as ``{session_id: binding}``.

    The mirror uses the count to decide whether messages need a session label: one session is
    the normal case and a label would be noise, two or more and an unlabelled stream is
    unreadable."""
    out = {}
    d = sessions_dir()
    want = slug(bridge)
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        data = _read_json(os.path.join(d, name))
        if isinstance(data, dict) and data.get("bridge") == want:
            out[name[:-5]] = data
    return out


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


def write_decision(bridge: str, request_id: str, decision: str, by=None,
                   answer: str = "") -> None:
    """Record a remote decision — an Allow/Deny, or an answer to a question.

    The waiting hook picks it up and deletes it."""
    payload = {"decision": decision, "by": by, "ts": time.time()}
    if answer:
        payload["answer"] = answer
    _write_private(decision_path(bridge, request_id), json.dumps(payload, ensure_ascii=False))


def sweep_decisions(bridge: str, ttl: float = DECISION_TTL) -> int:
    """Delete decisions no hook ever collected (it died, or Claude Code was killed)."""
    d = decisions_dir(bridge)
    now, removed = time.time(), 0
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(d, name)
        try:
            if now - os.stat(path).st_mtime > ttl:
                _unlink(path)
                removed += 1
        except OSError:
            pass
    return removed


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


def question_summary(inp) -> dict:
    """Flatten an ``AskUserQuestion`` input into something safe to show in a chat.

    Model-authored text, so every field is redacted and capped. Only the question text and the
    option *labels* are taken — never the free-form option descriptions, which are long and add
    nothing to "what is this session waiting for?".
    """
    inp = inp if isinstance(inp, dict) else {}
    questions = inp.get("questions")
    questions = questions if isinstance(questions, list) else []
    out = []
    for q in questions[:3]:
        if not isinstance(q, dict):
            continue
        options = [short(redact(o.get("label", "")), 60)
                   for o in (q.get("options") or [])[:6] if isinstance(o, dict)]
        out.append({
            "header": short(redact(q.get("header", "")), 40),
            "question": short(redact(q.get("question", "")), 240),
            "options": [o for o in options if o],
            "multi": bool(q.get("multiSelect")),
        })
    return {"questions": out}


def permission_detail(name: str, inp) -> str:
    """A few extra lines of context for an approval card.

    Deciding "allow" on a one-line summary is not really deciding, so this shows more than the
    bubble does — but still only known-safe fields, still redacted, still length-capped. The
    raw ``tool_input`` is never dumped.
    """
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return short(redact(inp.get("command", "")), 300)
    if name in ("Edit", "Write", "NotebookEdit", "Read"):
        return short(inp.get("file_path", ""), 200)
    if name in ("Grep", "Glob"):
        return short(redact(inp.get("pattern", "")), 200)
    if name in ("WebFetch",):
        return short(inp.get("url", ""), 200)
    if name == "WebSearch":
        return short(redact(inp.get("query", "")), 200)
    if name in ("Agent", "Task"):
        return short(redact(inp.get("description", "")), 200)
    if name.startswith("mcp__"):
        # MCP arguments are arbitrary and server-defined: name the fields, never their values.
        return "arguments: " + short(", ".join(sorted(inp)) or "none", 200)
    return ""


# --------------------------------------------------------------------------- connect recap

def _projects_dirs():
    """Where Claude Code keeps per-project transcripts (honouring CLAUDE_CONFIG_DIR)."""
    out = []
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        out.append(os.path.join(os.path.expanduser(cfg), "projects"))
    out.append(os.path.expanduser(os.path.join("~", ".claude", "projects")))
    return out


def transcript_for(session_id: str, cwd: str) -> str:
    """Path to a session's transcript, or "" — Claude Code stores it per working directory."""
    if not session_id or not cwd:
        return ""
    for base in _projects_dirs():
        for candidate in {cwd, os.path.realpath(cwd)}:
            path = os.path.join(base, candidate.replace("/", "-"), session_id + ".jsonl")
            if os.path.exists(path):
                return path
    return ""


def _record_text(rec: dict) -> str:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                          if isinstance(b, dict) and b.get("type") == "text")
    return ""


def recap(session_id: str, cwd: str, limit: int = RECAP_MESSAGES) -> str:
    """A short "here is where you are" digest of the conversation so far, or "".

    Connecting mid-session otherwise drops you into a stream with no context. This is the only
    transcript read in the project: a one-off, on an explicit user action, bounded to the tail
    of the file — the live mirror never touches it.
    """
    path = transcript_for(session_id, cwd)
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - RECAP_TAIL_BYTES))
            tail = f.read()
    except OSError:
        return ""
    turns = []
    for raw in tail.split(b"\n")[1:]:            # first line may be a partial record
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line.decode("utf-8", "ignore"))
        except (ValueError, TypeError):
            continue
        kind = rec.get("type")
        if kind not in ("user", "assistant"):
            continue
        text = _record_text(rec).strip()
        if not text:
            continue                              # tool results and tool calls carry no text
        turns.append((kind, text))
    if not turns:
        return ""
    lines = []
    for kind, text in turns[-limit:]:
        who = "🖥️ You" if kind == "user" else "🤖"
        body = text if len(text) <= RECAP_CHARS else text[:RECAP_CHARS - 1] + "…"
        lines.append(f"{who}: {body}")
    return "\n\n".join(lines)


# --------------------------------------------------------------------------- hook adapter

def prompt_text(payload: dict) -> str:
    """The submitted prompt from a ``UserPromptSubmit`` payload.

    The field is ``prompt``. It is read with a fallback because getting this wrong is not a
    cosmetic bug: an empty string classifies every turn as terminal, so Telegram-originated
    turns get mirrored *as well as* forwarded by the attach path, and every message arrives
    twice. Verified against a real payload, not a doc summary.
    """
    for key in ("prompt", "user_input", "user_prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def failure_text(payload: dict) -> str:
    """A human-readable reason from a ``StopFailure`` payload (the field is ``error``)."""
    for key in ("error", "error_message", "error_type"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return short(redact(value), 400)
        if isinstance(value, dict):
            for inner in ("message", "type"):
                if isinstance(value.get(inner), str) and value[inner].strip():
                    return short(redact(value[inner]), 400)
    return "unknown error"


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
    # Deliberately nothing for `clear` and `compact`: they keep the connection, and they must
    # NOT touch the turn origin. Auto-compaction fires in the MIDDLE of a long turn, so
    # resetting the origin here would reclassify a Telegram-originated turn as terminal and
    # start mirroring messages the attach path is already forwarding. The origin is owned by
    # UserPromptSubmit alone, which is the only event that actually starts a turn.


def _handle_session_end(payload: dict, binding: dict) -> None:
    reason = payload.get("reason", "")
    if reason in NON_TERMINAL_END_REASONS:
        return                                   # /clear and session switches are not an exit
    bridge, session_id = binding["bridge"], payload.get("session_id", "")
    _emit(bridge, session_id, "session_end", reason=reason)
    unbind_session(session_id, bridge)


def _await_decision(bridge: str, request_id: str, timeout: float,
                    valid=("allow", "deny")):
    """Block until the bridge records a decision for *request_id*, or until *timeout*.

    Polling a file is deliberate: the hook must never open a second Telegram connection — the
    long-running bridge owns the only one — and it must not depend on signals or sockets.
    """
    path = decision_path(bridge, request_id)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        data = _read_json(path)
        if isinstance(data, dict) and data.get("decision") in valid:
            _unlink(path)
            return data
        if time.monotonic() >= deadline:
            return None
        time.sleep(PERMISSION_POLL)


def _handle_question(payload: dict, binding: dict):
    """Let the chat answer a question Claude asked with its own picker.

    Claude Code has no hook output that supplies a tool *result*, so the answer cannot be handed
    to ``AskUserQuestion`` directly. It can be carried back the documented way instead: block the
    tool with ``permissionDecision: "deny"`` and put the user's choice in
    ``permissionDecisionReason``, which Claude Code shows to the model, which then continues with
    that choice. The question is never asked twice — the reason says so explicitly.

    Every failure path returns None, which leaves the tool call alone and lets Claude Code show
    its own picker at the terminal exactly as it would without this hook.
    """
    bridge = binding["bridge"]
    session_id = payload.get("session_id", "")
    summary = question_summary(payload.get("tool_input"))

    if not binding.get("permissions", True) or not consumer_alive(bridge):
        # Surface only: either remote decisions are off, or nobody is there to answer.
        _emit(bridge, session_id, "question",
              tool_use_id=payload.get("tool_use_id", ""),
              tool_name=short(payload.get("tool_name", ""), 40), **summary)
        return None
    if not summary.get("questions"):
        return None                                   # nothing recognizable to ask

    request_id = os.urandom(8).hex()
    timeout = float(binding.get("question_timeout") or QUESTION_TIMEOUT)
    if not _emit(bridge, session_id, "question_request", request_id=request_id,
                 tool_use_id=payload.get("tool_use_id", ""),
                 tool_name=short(payload.get("tool_name", ""), 40),
                 timeout=timeout, **summary):
        return None                                   # spool full → fall back to the terminal

    decision = _await_decision(bridge, request_id, timeout, valid=("answer",))
    if decision is None:
        _emit(bridge, session_id, "question_expired", request_id=request_id)
        return None

    answer = short(str(decision.get("answer") or "").strip(), 800)
    if not answer:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"The user answered this question from Telegram: {answer}. "
            "Treat that as their answer, continue with it, and do not ask again."),
    }}


def _handle_permission(payload: dict, binding: dict):
    """Ask Telegram to decide, or just report that a decision is pending.

    Returns the hook's JSON output (a decision) or None, which leaves Claude Code's normal
    permission flow completely untouched — that is the fallback for every failure mode here:
    no bridge, mirroring disabled, nobody answers in time.
    """
    bridge = binding["bridge"]
    session_id = payload.get("session_id", "")
    tool = payload.get("tool_name", "")
    summary = tool_summary(tool, payload.get("tool_input"))

    if not binding.get("permissions", True):
        _emit(bridge, session_id, "permission", summary=summary, tool_name=short(tool, 40))
        return None
    if not consumer_alive(bridge):
        # Nobody is reading the spool, so nobody can answer. Do not hold the session hostage.
        return None

    request_id = os.urandom(8).hex()
    timeout = float(binding.get("permission_timeout") or PERMISSION_TIMEOUT)
    if not _emit(bridge, session_id, "permission_request", request_id=request_id,
                 tool_name=short(tool, 40), summary=summary,
                 detail=permission_detail(tool, payload.get("tool_input")),
                 timeout=timeout):
        return None                                   # spool full → fall back to the terminal

    decision = _await_decision(bridge, request_id, timeout)
    if decision is None:
        # Tell the chat the buttons are dead, then let the terminal prompt appear as normal.
        _emit(bridge, session_id, "permission_expired", request_id=request_id)
        return None

    out = {"hookEventName": "PermissionRequest", "decision": decision["decision"]}
    if decision["decision"] == "deny":
        out["reason"] = "Denied from Telegram."
    return {"hookSpecificOutput": out}


def handle(payload: dict):
    """Translate one Claude Code hook payload into (at most) one spooled mirror event.

    Returns the hook's JSON output, or None for "say nothing". Only PermissionRequest ever
    returns anything."""
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id", "")

    # SessionStart runs before/independently of any binding — it is what clears one.
    if event == "SessionStart":
        _handle_session_start(payload)
        return None

    binding = session_binding(session_id)
    if binding is None:
        return None                               # not mirrored → the common case, ~0 work
    bridge = binding["bridge"]

    if event == "SessionEnd":
        _handle_session_end(payload, binding)
        return None

    if event == "UserPromptSubmit":
        text = prompt_text(payload)
        if not text.strip():
            # We could not read the prompt — an unexpected payload shape, or an empty submit.
            # Do NOT touch the origin: guessing "terminal" here is what makes a Telegram turn
            # get mirrored on top of the attach path's copy, i.e. everything twice.
            return None
        origin = classify_origin(text, binding.get("origins") or ())
        set_origin(bridge, session_id, origin, payload.get("prompt_id", ""))
        if origin == "terminal":
            _emit(bridge, session_id, "prompt", text=text, cwd=payload.get("cwd", ""))
        return None

    # Everything below mirrors the LOCAL seat only. Telegram-originated turns are already
    # forwarded by the attach bridge; mirroring them here would double every message.
    if get_origin(bridge, session_id) != "terminal":
        return None

    if event == "MessageDisplay":
        _emit(bridge, session_id, "message",
              message_id=payload.get("message_id", ""),
              turn_id=payload.get("turn_id", ""),
              index=payload.get("index", 0),
              delta=payload.get("delta", "") or "",
              final=bool(payload.get("final")))
    elif event == "PreToolUse":
        tool = payload.get("tool_name", "")
        if tool in BLOCKING_TOOLS:
            # Claude Code stops here until a human answers, and emits no turn end. Ask the chat
            # to answer it; if that isn't possible, say what it is waiting for, or the chat
            # shows a session that looks busy forever.
            return _handle_question(payload, binding)
        else:
            _emit(bridge, session_id, "tool",
                  summary=tool_summary(tool, payload.get("tool_input")),
                  tool_use_id=payload.get("tool_use_id", ""))
    elif event == "PostToolUse":
        # Registered for the blocking tools ONLY (see install.EVENT_MATCHERS): it tells us the
        # human answered. tool_output is deliberately never read.
        if payload.get("tool_name", "") in BLOCKING_TOOLS:
            _emit(bridge, session_id, "question_answered",
                  tool_use_id=payload.get("tool_use_id", ""))
    elif event == "PostToolUseFailure":
        _emit(bridge, session_id, "tool_failed",
              summary=tool_summary(payload.get("tool_name", ""), payload.get("tool_input")),
              error=short(redact(payload.get("error", "")), 160))
    elif event == "PermissionRequest":
        return _handle_permission(payload, binding)
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
    elif event == "PreCompact":
        _emit(bridge, session_id, "compact_start", trigger=short(payload.get("trigger", ""), 20))
    elif event == "PostCompact":
        _emit(bridge, session_id, "compact_end", trigger=short(payload.get("trigger", ""), 20))
    elif event == "Elicitation":
        _emit(bridge, session_id, "elicitation",
              elicitation_id=short(payload.get("elicitation_id", ""), 64),
              server_name=short(payload.get("server_name", ""), 40),
              prompt=short(redact(payload.get("prompt", "")), 240))
    elif event == "ElicitationResult":
        _emit(bridge, session_id, "elicitation_done",
              elicitation_id=short(payload.get("elicitation_id", ""), 64))
    elif event == "Stop":
        _emit(bridge, session_id, "turn_end",
              last_assistant_message=payload.get("last_assistant_message", "") or "")
    elif event == "StopFailure":
        _emit(bridge, session_id, "turn_failed",
              error=failure_text(payload),
              partial=short(redact(payload.get("last_assistant_message", "")), 400))
    return None


def hook_main(stdin=None, stdout=None) -> int:
    """Entry point for the registered hook command. Always exits 0 (fail open).

    Prints JSON only for a PermissionRequest that was actually decided remotely; every other
    event, and every failure, produces no output at all — which leaves Claude Code's own
    behaviour exactly as it would have been without this hook."""
    try:
        payload = json.load(stdin if stdin is not None else sys.stdin)
        out = handle(payload) if isinstance(payload, dict) else None
        if out:
            (stdout if stdout is not None else sys.stdout).write(
                json.dumps(out, ensure_ascii=False))
    except Exception:
        pass                                      # a broken mirror never breaks Claude Code
    return 0


if __name__ == "__main__":                        # executed directly: python3 -S -E core.py
    sys.exit(hook_main())
