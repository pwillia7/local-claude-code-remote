"""Remote Control consumer — turn spooled hook events into live Telegram messages.

The hook side (:mod:`agent2telegram.remote_control.core`) only writes small JSON files. This is
where the Telegram work happens, inside the long-running bridge process, reusing the machinery
the attach bridge already has: the durable send path, the one-line status bubble, the typing
indicator and the retry/flood-control handling in :class:`~agent2telegram.telegram.TelegramClient`.

Streaming model (mirrors Claude Code's ``MessageDisplay`` events):

  * the first delta of an assistant message creates a Telegram message;
  * later deltas are appended to a buffer and the Telegram message is *edited*, at most once
    every :data:`EDIT_INTERVAL` seconds so a burst of deltas cannot flood the Bot API;
  * ``final`` flushes immediately and closes the message, even when the final delta is empty;
  * a message that outgrows Telegram's size limit is closed at a natural boundary and continued
    in a new message — content is never truncated.

Text is sent as **plain text** while streaming and stays plain when finalized: a half-written
Markdown span would make Telegram reject the edit, and a rejected edit would silently lose the
tail of an answer.
"""
from __future__ import annotations

import logging
import time

from . import core
from ..telegram import MAX_MESSAGE_LEN

log = logging.getLogger("agent2telegram.remote_control")

#: Minimum spacing between edits of the same streaming Telegram message.
EDIT_INTERVAL = 0.6
#: Leave room for Telegram's own accounting (it counts UTF-16 code units, we count characters).
CHUNK_LIMIT = MAX_MESSAGE_LEN - 200
#: Most events consumed per tick — keeps one bridge cycle bounded even after a long outage.
DRAIN_LIMIT = 400
#: Above this the spool is being produced faster than it is consumed (or was orphaned); the
#: oldest events are dropped so the queue cannot grow without bound.
DROP_ABOVE = core.MAX_PENDING * 2
#: Safety net: a turn that never sends Stop or StopFailure (Claude Code was killed, the machine
#: slept) must not leave "typing…" and a status bubble lit forever.
IDLE_DONE = 180.0


def _split_head(text: str, limit: int) -> tuple[str, str]:
    """Split *text* at the latest natural boundary inside *limit* (paragraph → line → word)."""
    window = text[:limit]
    for sep in ("\n\n", "\n", " "):
        cut = window.rfind(sep)
        if cut > limit * 0.5:
            return text[:cut].rstrip(), text[cut:].lstrip("\n")
    return text[:limit], text[limit:]


class _LiveMessage:
    """One assistant message being streamed into one (or more) Telegram messages."""

    __slots__ = ("buf", "mid", "shown", "last_edit", "closed")

    def __init__(self) -> None:
        self.buf = ""
        self.mid = None          # current Telegram message id (None → not created yet)
        self.shown = ""          # what that Telegram message currently displays
        self.last_edit = 0.0
        self.closed = False


class RemoteControlMirror:
    """Consumes one bridge's Remote Control spool.

    All Telegram I/O goes through callables supplied by the bridge, so there is exactly one
    implementation of the status bubble, the durable send queue and the typing indicator.
    """

    def __init__(self, bridge: str, *, send_plain_id, edit_plain, send_text,
                 status_push, status_clear, set_active, label: str = "") -> None:
        self.bridge = core.slug(bridge)
        self.label = label
        self._send_plain_id = send_plain_id
        self._edit_plain = edit_plain
        self._send_text = send_text
        self._status_push = status_push
        self._status_clear = status_clear
        self._set_active = set_active
        self._live: dict[str, _LiveMessage] = {}
        self._order: list[str] = []
        self._delivered = False        # did this turn already put assistant text in the chat?
        self._active = False
        self._last_event = 0.0

    # ---- lifecycle ---------------------------------------------------------
    def tick(self) -> int:
        """One bridge cycle: publish liveness, drain the spool, honour edit throttling."""
        core.touch_heartbeat(self.bridge)
        self._prune()
        handled = 0
        for path, event in core.read_events(self.bridge, DRAIN_LIMIT):
            if event is None:
                log.warning("remote-control: dropping malformed event %s", path)
                core.ack_event(path)               # a corrupt file must not wedge the queue
                continue
            try:
                self._apply(event)
            except Exception as e:                 # one bad event never stalls the rest
                log.warning("remote-control: event %s failed: %s", event.get("type"), e)
            core.ack_event(path)                   # payloads are not retained after forwarding
            handled += 1
        if handled:
            self._last_event = time.monotonic()
        elif self._active and time.monotonic() - self._last_event > IDLE_DONE:
            # No Stop/StopFailure ever arrived — end the turn ourselves rather than leave the
            # chat showing a session that is still working.
            log.info("remote-control: no events for %.0fs, ending the mirrored turn", IDLE_DONE)
            self._finalize_all()
            self._end_turn()
        self._flush()
        if handled:
            log.debug("remote-control: applied %d event(s)", handled)
        return handled

    def _prune(self) -> None:
        pending = core.pending_count(self.bridge)
        if pending <= DROP_ABOVE:
            return
        drop = pending - core.MAX_PENDING
        log.warning("remote-control: spool at %d events, dropping %d oldest", pending, drop)
        for path, _ in core.read_events(self.bridge, drop):
            core.ack_event(path)

    def _activate(self, on: bool) -> None:
        if on != self._active:
            self._active = on
            self._set_active(on)

    # ---- event dispatch ----------------------------------------------------
    def _apply(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "prompt":
            self._on_prompt(ev)
        elif kind == "message":
            self._on_message(ev)
        elif kind == "tool":
            self._activate(True)
            self._status_push(ev.get("summary") or "🛠️ tool")
        elif kind == "tool_failed":
            self._activate(True)
            summary = ev.get("summary") or "🛠️ tool"
            self._status_push("⚠️ Failed: " + core.short(summary.split(" ", 1)[-1], 50))
        elif kind == "subagent_start":
            self._activate(True)
            self._status_push(core.subagent_summary(ev.get("agent_type", "")))
        elif kind == "subagent_stop":
            self._status_push("🤖 Subagent completed")
        elif kind == "task_created":
            self._status_push("📋 Working: " + core.short(ev.get("task_name", ""), 60))
        elif kind == "task_completed":
            self._status_push("✅ Task completed: " + core.short(ev.get("task_name", ""), 60))
        elif kind == "permission":
            self._on_permission(ev)
        elif kind == "notification":
            self._on_notification(ev)
        elif kind == "turn_end":
            self._on_turn_end(ev)
        elif kind == "turn_failed":
            self._on_turn_failed(ev)
        elif kind == "session_end":
            self._on_session_end(ev)

    def _on_prompt(self, ev: dict) -> None:
        text = (ev.get("text") or "").strip()
        self._delivered = False
        self._activate(True)
        if text:
            # Plain text: a local prompt is arbitrary content and must never be re-parsed.
            self._send_text("🖥️ You:\n" + text, parse_mode=None)

    def _on_message(self, ev: dict) -> None:
        mid = ev.get("message_id") or ev.get("turn_id") or "message"
        state = self._live.get(mid)
        if state is None:
            state = self._live[mid] = _LiveMessage()
            self._order.append(mid)
            # A new assistant message means the trailing tool bubble must move below it.
            self._status_clear()
        self._activate(True)
        state.buf += ev.get("delta") or ""
        if ev.get("final"):
            state.closed = True
            self._emit(state, force=True)          # empty final delta still finalizes
            self._forget(mid)

    def _on_permission(self, ev: dict) -> None:
        self._send_text(
            "🔐 Waiting for permission\n\n"
            f"{ev.get('summary') or ev.get('tool_name') or 'a tool call'}\n\n"
            "Approve or deny it in the terminal — remote approval isn't supported yet.",
            parse_mode=None)

    def _on_notification(self, ev: dict) -> None:
        texts = {
            "idle_prompt": "🔔 Waiting for your input.",
            "agent_needs_input": "🔔 Needs your input to continue.",
            "agent_completed": "🔔 Finished working.",
        }
        text = texts.get(ev.get("notification_type", ""))
        if text:
            self._send_text(text, parse_mode=None)

    def _on_turn_end(self, ev: dict) -> None:
        self._finalize_all()
        # Backstop: if nothing reached the chat through MessageDisplay (hook not registered,
        # a mirror error, an answer rendered some other way), deliver the documented final
        # message. No transcript parsing, and never a second copy of streamed text.
        if not self._delivered:
            answer = (ev.get("last_assistant_message") or "").strip()
            if answer:
                self._send_text("🖥️ " + answer)
                log.info("remote-control: Stop backstop delivered the final answer")
        self._end_turn()

    def _on_turn_failed(self, ev: dict) -> None:
        self._finalize_all()
        etype = ev.get("error_type") or "unknown"
        emsg = (ev.get("error_message") or "").strip()
        self._send_text(f"⚠️ Turn ended with an error ({etype})"
                        + (f"\n\n{emsg}" if emsg else ""), parse_mode=None)
        self._end_turn()

    def _on_session_end(self, ev: dict) -> None:
        self._finalize_all()
        self._end_turn()

    def _end_turn(self) -> None:
        self._status_clear()
        self._activate(False)
        self._delivered = False

    # ---- streaming ---------------------------------------------------------
    def _forget(self, mid: str) -> None:
        self._live.pop(mid, None)
        if mid in self._order:
            self._order.remove(mid)

    def _flush(self) -> None:
        """Apply throttled edits for every message still streaming."""
        now = time.monotonic()
        for mid in list(self._order):
            state = self._live.get(mid)
            if state is None:
                continue
            if now - state.last_edit >= EDIT_INTERVAL or len(state.buf) > CHUNK_LIMIT:
                self._emit(state)

    def _finalize_all(self) -> None:
        for mid in list(self._order):
            state = self._live.get(mid)
            if state is not None:
                state.closed = True
                self._emit(state, force=True)
            self._forget(mid)

    def _emit(self, state: _LiveMessage, force: bool = False) -> None:
        """Push the buffer to Telegram, continuing into new messages when it outgrows one."""
        if not force and time.monotonic() - state.last_edit < EDIT_INTERVAL:
            return
        while len(state.buf) > CHUNK_LIMIT:
            head, tail = _split_head(state.buf, CHUNK_LIMIT)
            state.buf = head
            self._write(state)                    # close the current Telegram message on `head`
            state.buf, state.mid, state.shown = tail, None, ""
        self._write(state)

    def _write(self, state: _LiveMessage) -> None:
        text = state.buf
        if not text.strip():
            return                                 # nothing worth a Telegram message yet
        if text == state.shown:
            state.last_edit = time.monotonic()
            return
        if state.mid is None:
            mid = self._send_plain_id(text)
            if mid is None:
                return                             # send failed; retry on the next flush
            state.mid = mid
            # Metadata only — message CONTENT is never logged (see docs/SECURITY.md).
            log.info("MIRROR (stream) telegram msg %s, %d chars", mid, len(text))
        else:
            self._edit_plain(state.mid, text)
        state.shown = text
        state.last_edit = time.monotonic()
        self._delivered = True
