"""Local Remote Control — mirror a local interactive Claude Code session through Telegram.

Native Claude Code Remote Control is unavailable whenever the session's model backend is not
``api.anthropic.com`` — for example when Claude Code is routed through Claude Code Router (CCR)
or another LLM gateway. This package provides an unofficial local equivalent built entirely on
documented Claude Code hooks:

    Claude Code hooks → local event spool → Agent2Telegram bridge → Telegram

Permissions and questions can be decided from the chat, but only by a person pressing a button
or replying — never on a timer, a heuristic or the model's say-so. Nothing here scrapes a
terminal, opens a port, or reads a transcript on the live path (the one exception, the
connect-time recap, is bounded and explicit).

Modules:

  :mod:`.core`       the hook side — fast, standard-library only, local-only, fails open
  :mod:`.mirror`     the Telegram side, driven from the bridge's outbound loop
  :mod:`.cli`        the ``remote-control`` command tree
  :mod:`.install`    installer, uninstaller and doctor
  :mod:`.supervise`  starting the bridge without ever starting a second poller
"""
from __future__ import annotations

from .core import (                                    # noqa: F401  (public surface)
    bind_session,
    classify_origin,
    get_origin,
    handle,
    hook_main,
    is_enabled,
    session_binding,
    set_origin,
    unbind_session,
)

__all__ = [
    "bind_session", "classify_origin", "get_origin", "handle", "hook_main",
    "is_enabled", "session_binding", "set_origin", "unbind_session",
    "RemoteControlMirror",
]


def __getattr__(name):
    # Imported lazily: the mirror pulls in the Telegram client, which the hot hook path
    # (and simple state queries) must never pay for.
    if name == "RemoteControlMirror":
        from .mirror import RemoteControlMirror
        return RemoteControlMirror
    raise AttributeError(name)
