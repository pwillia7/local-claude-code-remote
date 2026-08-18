"""Local Remote Control — mirror a local interactive Claude Code session through Telegram.

Native Claude Code Remote Control is unavailable whenever the session's model backend is not
``api.anthropic.com`` — for example when Claude Code is routed through Claude Code Router (CCR)
or another LLM gateway. This package provides an unofficial local equivalent built entirely on
documented Claude Code hooks:

    Claude Code hooks → local event spool → Agent2Telegram bridge → Telegram

Nothing here scrapes a terminal, parses a transcript as a primary source, opens a port, or
approves a permission on your behalf. See :mod:`.core` for the hook side (fast, local-only),
:mod:`.mirror` for the Telegram side and :mod:`.install` for the installer.
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
