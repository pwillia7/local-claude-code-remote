"""``agent2telegram remote-control …`` — enable, inspect and install local Remote Control.

Subcommands::

    remote-control toggle <session-id>     turn mirroring on/off for one Claude session
    remote-control on|off <session-id>     idempotent forms of the same thing
    remote-control status [<session-id>]   what is bound, and whether the bridge is consuming
    remote-control hook                    read ONE hook payload on stdin (fast path)
    remote-control session-start           alias of `hook`, kept for older installs
    remote-control install                 install the Skill + merge the hooks into settings.json
    remote-control uninstall               remove only what this project installed
    remote-control doctor                  check the whole chain

``toggle`` is what the Skill calls, so its stdout is a stable contract:
``REMOTE_ENABLED`` / ``REMOTE_ENABLED_WITH_WARNING`` / ``REMOTE_DISABLED`` / ``ERROR: …``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import core

#: Default user-facing name. The Skill passes ``--label Qwen`` (or whatever the local model is)
#: so the wording matches the harness without the package knowing anything about it.
DEFAULT_LABEL = "Remote Control"


# --------------------------------------------------------------------------- discovery

def tmux_session() -> str:
    """Name of the tmux session this process sits in ("" when not inside tmux)."""
    if not os.environ.get("TMUX"):
        return ""
    cmd = ["tmux", "display-message", "-p"]
    if os.environ.get("TMUX_PANE"):
        cmd += ["-t", os.environ["TMUX_PANE"]]
    cmd += ["#S"]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def config_dir() -> Path:
    from ..config import config_path
    return config_path().parent


def find_config(explicit: str = "", session: str = "") -> tuple[Path | None, dict]:
    """Locate the bridge config to mirror through.

    Order: an explicit ``--config``; ``$AGENT2TELEGRAM_CONFIG``; otherwise the config in the
    config directory whose ``tmux_session`` matches ``--tmux-session`` or the seat we are in.
    """
    if explicit:
        p = Path(explicit).expanduser()
        data = _load(p)
        return (p, data) if data else (None, {})
    if os.environ.get("AGENT2TELEGRAM_CONFIG"):
        p = Path(os.environ["AGENT2TELEGRAM_CONFIG"]).expanduser()
        data = _load(p)
        if data:
            return p, data
    seat = session or tmux_session()
    if not seat:
        return None, {}
    d = config_dir()
    if not d.is_dir():
        return None, {}
    for p in sorted(d.glob("*.json")):
        data = _load(p)
        if data.get("tmux_session") == seat:
            return p, data
    return None, {}


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _origins(cfg: dict) -> tuple:
    return tuple(p for p in ((cfg.get("origin_prefix") or "").strip(),) if p)


def _notify(cfg: dict, text: str) -> bool:
    """Send one message through the bridge's own Telegram client (token never leaves here)."""
    owner = (cfg.get("allowed_user_ids") or [None])[0]
    if not cfg.get("token") or owner is None:
        return False
    try:
        from ..telegram import TelegramClient
        TelegramClient(cfg["token"]).send_message(owner, text)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- commands

def cmd_toggle(args) -> int:
    session_id = (args.session_id or "").strip()
    if not session_id:
        print("ERROR: no Claude session id")
        return 1
    want = getattr(args, "want", None)
    binding = core.session_binding(session_id)

    if binding is not None and want is not True:
        cfg_path, cfg = find_config(args.config, args.tmux_session)
        if cfg:
            _notify(cfg, f"🔴 **{args.label} disconnected**")
        core.unbind_session(session_id, binding.get("bridge", ""))
        print("REMOTE_DISABLED")
        return 0
    if binding is not None:
        print("REMOTE_ENABLED")                    # already on and `on` was requested
        return 0
    if want is False:
        print("REMOTE_DISABLED")
        return 0

    cfg_path, cfg = find_config(args.config, args.tmux_session)
    if not cfg_path:
        print("ERROR: no Agent2Telegram bridge matches this tmux session "
              "(pass --config or --tmux-session)")
        return 1
    bridge = cfg.get("tmux_session") or cfg_path.stem

    # Enabling mirroring promises that a phone will show this session. Keep the promise: if
    # nothing is draining the spool, start the bridge (never a second one — see supervise).
    warning = ""
    if not getattr(args, "no_bridge_start", False):
        from . import supervise
        started, message = supervise.ensure_running(bridge, str(cfg_path),
                                                    python=sys.executable)
        if not started:
            warning = message

    permissions = not getattr(args, "no_permission_prompts", False)
    cwd = os.getcwd()                       # the Skill runs inside the session, so this is its cwd
    core.bind_session(session_id, bridge=bridge, config_path=str(cfg_path),
                      origins=_origins(cfg), label=args.label, cwd=cwd,
                      permissions=permissions,
                      permission_timeout=getattr(args, "permission_timeout",
                                                 core.PERMISSION_TIMEOUT),
                      question_timeout=getattr(args, "question_timeout",
                                               core.QUESTION_TIMEOUT))
    # Connecting mid-session otherwise drops the phone into a stream with no context.
    if not getattr(args, "no_recap", False):
        try:
            text = core.recap(session_id, cwd)
            if text:
                core.write_event(bridge, {"type": "recap", "session_id": session_id,
                                          "text": text, "cwd": cwd})
        except Exception:
            pass                            # a missing recap must never block connecting
    extra = ("\nPermissions and questions can be answered from here."
             if permissions else "\nPermissions and questions are notification-only.")
    ok = _notify(cfg, f"🟢 **{args.label} connected**\n\n"
                      "Local activity will now be mirrored here." + extra)
    if warning:
        print(f"WARNING: {warning}")
    print("REMOTE_ENABLED" if (ok and not warning) else "REMOTE_ENABLED_WITH_WARNING")
    return 0


def cmd_status(args) -> int:
    session_id = (args.session_id or "").strip()
    if session_id:
        binding = core.session_binding(session_id)
        if binding is None:
            print(f"disabled  session={session_id}")
            return 1
        bridge = binding["bridge"]
        from . import supervise
        state, _ = supervise.status(bridge, binding.get("config", ""))
        print(f"enabled   session={session_id} bridge={bridge} "
              f"origin={core.get_origin(bridge, session_id)} "
              f"pending={core.pending_count(bridge)} "
              f"consumer={state} "
              f"permissions={'remote' if binding.get('permissions', True) else 'notify-only'}")
        return 0
    root = Path(core.sessions_dir())
    rows = sorted(root.glob("*.json")) if root.is_dir() else []
    if not rows:
        print("no sessions have Remote Control enabled")
        return 0
    for p in rows:
        data = _load(p)
        bridge = data.get("bridge", "?")
        print(f"enabled   session={p.stem} bridge={bridge} "
              f"pending={core.pending_count(bridge)} "
              f"consumer={'live' if core.consumer_alive(bridge) else 'down'}")
    return 0


def cmd_hook(_args) -> int:
    return core.hook_main()


def cmd_doctor(args) -> int:
    from .install import doctor
    return doctor(args)


def cmd_install(args) -> int:
    from .install import install
    return install(args)


def cmd_uninstall(args) -> int:
    from .install import uninstall
    return uninstall(args)


# --------------------------------------------------------------------------- argparse

def add_parser(sub) -> None:
    """Register the ``remote-control`` command tree on the main parser."""
    rc = sub.add_parser("remote-control",
                        help="mirror a local Claude Code session to Telegram (hook-based)")
    rcs = rc.add_subparsers(dest="rc_command", required=True)

    def _common(p, session=True):
        if session:
            p.add_argument("session_id", nargs="?", default="",
                           help="Claude session id (${CLAUDE_SESSION_ID})")
        p.add_argument("--config", default="", help="path to a specific bridge config")
        p.add_argument("--tmux-session", default="",
                       help="bridge to use, by tmux session name (default: the current seat)")
        p.add_argument("--label", default=DEFAULT_LABEL,
                       help="user-facing name used in the connect/disconnect notices")
        p.add_argument("--no-permission-prompts", action="store_true",
                       help="turn OFF all remote decisions — permission Allow/Deny buttons and "
                            "answering Claude's questions. Both are then reported only, and "
                            "decided at the terminal.")
        p.add_argument("--question-timeout", type=float, default=core.QUESTION_TIMEOUT,
                       help="seconds to wait for a remote answer to a question before falling "
                            f"back to the terminal picker (default: {core.QUESTION_TIMEOUT:.0f})")
        p.add_argument("--permission-timeout", type=float, default=core.PERMISSION_TIMEOUT,
                       help="seconds to wait for a remote Allow/Deny before falling back to "
                            f"the terminal prompt (default: {core.PERMISSION_TIMEOUT:.0f})")
        p.add_argument("--no-bridge-start", action="store_true",
                       help="do not start the Agent2Telegram bridge if it is not running")
        p.add_argument("--no-recap", action="store_true",
                       help="do not send a digest of the conversation so far on connect")
        return p

    _common(rcs.add_parser("toggle", help="turn mirroring on or off for one session"))
    _common(rcs.add_parser("on", help="turn mirroring on (idempotent)"))
    _common(rcs.add_parser("off", help="turn mirroring off (idempotent)"))
    _common(rcs.add_parser("status", help="show what is mirrored right now"))
    rcs.add_parser("hook", help="process ONE Claude Code hook payload from stdin")
    rcs.add_parser("session-start", help="alias of 'hook' (compatibility)")

    ins = rcs.add_parser("install", help="install the Skill and merge the hooks")
    ins.add_argument("--claude-config-dir", default="",
                     help="Claude config dir (default: $CLAUDE_CONFIG_DIR, else ~/.claude)")
    ins.add_argument("--agent2telegram-config", default="",
                     help="bridge config to bind the Skill to (default: auto-detect)")
    ins.add_argument("--tmux-session", default="",
                     help="bridge to use, by tmux session name")
    ins.add_argument("--skill-name", default="local-remote",
                     help="name of the installed Skill (default: local-remote)")
    ins.add_argument("--label", default=DEFAULT_LABEL,
                     help="user-facing name shown by the Skill and its notices")
    ins.add_argument("--python", default="", help="interpreter for the hook command")
    ins.add_argument("--no-permission-prompts", action="store_true",
                     help="install with remote decisions OFF — no permission Allow/Deny buttons "
                          "and no answering Claude's questions; both are reported only")
    ins.add_argument("--permission-timeout", type=float, default=core.PERMISSION_TIMEOUT,
                     help="seconds a permission request waits for a remote answer "
                          f"(default: {core.PERMISSION_TIMEOUT:.0f})")
    ins.add_argument("--question-timeout", type=float, default=core.QUESTION_TIMEOUT,
                     help="seconds a question waits for a remote answer "
                          f"(default: {core.QUESTION_TIMEOUT:.0f})")
    ins.add_argument("--dry-run", action="store_true", help="report changes without making them")

    un = rcs.add_parser("uninstall", help="remove this project's hooks, Skill and state")
    un.add_argument("--claude-config-dir", default="")
    un.add_argument("--skill-name", default="local-remote")
    un.add_argument("--keep-state", action="store_true", help="leave the state directory in place")
    un.add_argument("--dry-run", action="store_true")

    doc = rcs.add_parser("doctor", help="check hooks, Skill, bridge and spool health")
    doc.add_argument("--claude-config-dir", default="")
    doc.add_argument("--skill-name", default="local-remote")
    doc.add_argument("--config", default="")
    doc.add_argument("--tmux-session", default="")


def run(args) -> int:
    cmd = getattr(args, "rc_command", "")
    if cmd in ("toggle", "on", "off"):
        args.want = {"toggle": None, "on": True, "off": False}[cmd]
        return cmd_toggle(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd in ("hook", "session-start"):
        return cmd_hook(args)
    if cmd == "install":
        return cmd_install(args)
    if cmd == "uninstall":
        return cmd_uninstall(args)
    if cmd == "doctor":
        return cmd_doctor(args)
    print(f"unknown remote-control command: {cmd}", file=sys.stderr)
    return 2
