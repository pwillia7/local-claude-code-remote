"""Make sure the bridge is actually running before we promise to mirror anything.

Turning Remote Control on is a promise that a phone will show the session. That promise is
empty if nothing is draining the spool, and "I enabled it but saw nothing" is a miserable
failure mode — so the toggle starts the bridge when it has to.

The one hard rule: **never start a second poller for the same bot token.** Telegram hands each
update to exactly one `getUpdates` consumer, so two bridges make messages vanish at random.
Everything here is built around not doing that:

  * a fresh consumer heartbeat means a bridge is already draining this spool — do nothing;
  * an `agent2telegram run` process that matches this config but is *not* heartbeating means an
    old or wedged bridge — say so and start nothing;
  * only when neither is true do we launch one, under an exclusive lock.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import core

#: A heartbeat this fresh means the bridge is consuming right now (it beats every ~0.4 s).
#: Deliberately much tighter than ``core.CONSUMER_GRACE``, which answers a different question
#: ("may the spool keep growing?") and can afford to be generous.
ALIVE_GRACE = 5.0
#: A live bridge can stop beating for a while inside a Telegram flood-control sleep, so when a
#: process exists but looks quiet, give it this long to beat again before calling it wedged.
RECHECK = 8.0
#: How long to wait for a bridge we just started to publish its first heartbeat.
START_TIMEOUT = 25.0
#: A start lock older than this was left by a crashed process.
LOCK_TTL = 120.0


def tmux_available() -> bool:
    try:
        return subprocess.run(["tmux", "-V"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def session_name(bridge: str) -> str:
    return "a2t-" + core.slug(bridge)


def _tmux_has(name: str) -> bool:
    try:
        return subprocess.run(["tmux", "has-session", "-t", name],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def running_process(config_path: str):
    """Command line of an `agent2telegram run` already serving *config_path*.

    Returns ``""`` for "definitely none" and ``None`` for "could not tell" — the difference
    matters, because "could not tell" must never be read as permission to start a second
    poller. A bridge started without ``--config`` serves the default config, so it counts as
    a match for the default config too.
    """
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        from ..config import config_path as default_config_path
        is_default = Path(config_path or "").expanduser() == default_config_path()
    except Exception:
        is_default = False
    for line in out.stdout.splitlines():
        if "agent2telegram" not in line or " run" not in line:
            continue
        if "remote-control" in line or "ps -eo" in line:
            continue
        if config_path and config_path in line:
            return line.strip()
        if "--config" not in line and is_default:
            return line.strip()
    return ""


def status(bridge: str, config_path: str = "") -> tuple[str, str]:
    """``("consuming"|"stale"|"stopped", detail)`` for this bridge.

    The process list decides whether anything is *running*; the heartbeat decides whether it is
    *consuming*. Using the heartbeat alone was wrong: a bridge killed a second ago still has a
    heartbeat one second old, so "just died" was indistinguishable from "alive".
    """
    fresh = core.consumer_alive(bridge, ALIVE_GRACE)
    proc = running_process(config_path)
    if proc is None:                       # cannot inspect processes → the heartbeat is all we have
        return ("consuming", "") if fresh else ("stopped", "")
    if not proc:
        return "stopped", ""               # nothing is running, whatever the heartbeat says
    if fresh or _wait_alive(bridge, RECHECK):
        return "consuming", ""             # running, and beating (possibly after a backoff)
    return "stale", proc


def _acquire_lock(bridge: str) -> bool:
    """One starter at a time, so two toggles racing cannot launch two bridges."""
    path = os.path.join(core.bridge_dir(bridge), "start.lock")
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        if os.path.exists(path) and time.time() - os.stat(path).st_mtime > LOCK_TTL:
            os.unlink(path)                      # left behind by a crash
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except OSError:
        return False


def _release_lock(bridge: str) -> None:
    try:
        os.unlink(os.path.join(core.bridge_dir(bridge), "start.lock"))
    except OSError:
        pass


def _launch(argv: list, bridge: str, log_path: Path) -> str:
    """Start the bridge detached. Returns how it was started, for the user-facing report."""
    if tmux_available():
        name = session_name(bridge)
        if _tmux_has(name):
            return f"tmux session '{name}' (already present)"
        shell_cmd = f"{shlex.join(argv)} 2>&1 | tee -a {shlex.quote(str(log_path))}"
        subprocess.run(["tmux", "new-session", "-d", "-s", name, shell_cmd],
                       capture_output=True, timeout=15)
        return f"tmux session '{name}'"
    # No tmux (so not attach mode): detach from this shell and log to the state directory.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8")
    subprocess.Popen(argv, stdout=handle, stderr=handle, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    return f"background process (logging to {log_path})"


def ensure_running(bridge: str, config_path: str = "", *, python: str = "",
                   timeout: float = START_TIMEOUT) -> tuple[bool, str]:
    """Guarantee something is draining this bridge's spool. Returns ``(ok, message)``."""
    state, detail = status(bridge, config_path)
    if state == "consuming":
        return True, "bridge already running"
    if state == "stale":
        return False, ("a bridge process is running but is not draining the Remote Control "
                       "spool — it predates this feature or is wedged. Restart it:\n  " + detail)

    if not _acquire_lock(bridge):
        # Someone else is starting it right now; just wait for their heartbeat.
        return (_wait_alive(bridge, timeout),
                "bridge starting (another process got there first)")
    try:
        argv = [python or sys.executable or "python3", "-m", "agent2telegram", "run"]
        if config_path:
            argv += ["--config", config_path]
        how = _launch(argv, bridge, Path(core.state_dir()) / "run.log")
        if _wait_alive(bridge, timeout):
            return True, f"started the bridge in {how}"
        return False, (f"started the bridge in {how}, but it has not begun consuming after "
                       f"{timeout:.0f}s — check the log")
    finally:
        _release_lock(bridge)


def _wait_alive(bridge: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if core.consumer_alive(bridge, ALIVE_GRACE):
            return True
        time.sleep(0.3)
    return core.consumer_alive(bridge, ALIVE_GRACE)
