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

import html
import logging
import threading
import time

from . import core
from ..telegram import MAX_MESSAGE_LEN, markdown_to_html

log = logging.getLogger("agent2telegram.remote_control")

#: Minimum spacing between edits of the same streaming Telegram message.
EDIT_INTERVAL = 0.6
#: Chunk on the RAW text, well under the wire limit: rendering Markdown to HTML only ever makes
#: a message longer (tags, entity escapes), and the rendered form still has to fit.
CHUNK_LIMIT = 3000
#: Telegram's real ceiling. If a rendered chunk somehow exceeds it we send the raw text instead.
WIRE_LIMIT = MAX_MESSAGE_LEN
#: Most events consumed per tick — keeps one bridge cycle bounded even after a long outage.
DRAIN_LIMIT = 400
#: Above this the spool is being produced faster than it is consumed (or was orphaned); the
#: oldest events are dropped so the queue cannot grow without bound.
DROP_ABOVE = core.MAX_PENDING * 2
#: Safety net: a turn that never sends Stop or StopFailure (Claude Code was killed, the machine
#: slept) must not leave "typing…" and a status bubble lit forever.
IDLE_DONE = 180.0
#: Inline keyboard removed by replacing it with an empty one.
NO_KEYBOARD = {"inline_keyboard": []}


def _render(text: str) -> str | None:
    """The agent's Markdown as Telegram HTML, or None when it will not fit the wire limit.

    ``markdown_to_html`` only emits a tag for a *closed* span, so a half-streamed ``**bold`` or
    an unterminated code fence stays literal text rather than becoming unbalanced HTML.
    """
    try:
        rendered = markdown_to_html(text)
    except Exception:
        return None
    return rendered if len(rendered) <= WIRE_LIMIT else None


def _replace_header(text: str, header: str) -> str:
    """Swap the first line of an approval card for its outcome, keeping the details below.

    Telegram hands the message back as plain text (entities stripped), so the body is escaped
    rather than re-rendered."""
    body = "\n".join((text or "").splitlines()[1:]).strip("\n")
    return header + (f"\n\n{html.escape(body)}" if body else "")


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
                 status_push, status_clear, set_active, answer_callback_query=None,
                 label: str = "") -> None:
        self.bridge = core.slug(bridge)
        self.label = label
        self._send_plain_id = send_plain_id
        self._edit_plain = edit_plain
        self._answer_callback_query = answer_callback_query
        self._send_text = send_text
        self._status_push = status_push
        self._status_clear = status_clear
        self._set_active = set_active
        self._live: dict[str, _LiveMessage] = {}
        self._order: list[str] = []
        self._delivered = False        # did this turn already put assistant text in the chat?
        self._active = False
        self._last_event = 0.0
        # Approval cards awaiting a button press. Touched by the bridge's outbound thread (when
        # a card is posted) AND by its inbound thread (when the press arrives), so it is locked.
        self._pending_perms: dict[str, dict] = {}
        self._perm_lock = threading.Lock()

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
        core.sweep_decisions(self.bridge)
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
        elif kind == "permission_request":
            self._on_permission_request(ev)
        elif kind == "permission_expired":
            self._resolve_permission(ev.get("request_id", ""),
                                     "⌛ <b>Expired</b> — answer it at the terminal.")
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

    def _on_permission_request(self, ev: dict) -> None:
        """Post an approval card with Allow/Deny buttons; the hook is blocked waiting on it."""
        request_id = ev.get("request_id", "")
        if not request_id:
            return
        self._activate(True)
        self._status_clear()                       # the card must be the last thing in the chat
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Allow", "callback_data": f"p:{request_id}:a"},
            {"text": "⛔ Deny", "callback_data": f"p:{request_id}:d"},
        ]]}
        mid = self._send_plain_id(self._permission_card(ev), parse_mode="HTML",
                                  reply_markup=keyboard)
        if mid is None:
            log.warning("remote-control: could not post the approval card; "
                        "the terminal prompt will handle it")
            return
        with self._perm_lock:
            self._pending_perms[request_id] = {"mid": mid, "ts": time.monotonic()}
        log.info("MIRROR (permission) card %s for %s", mid, ev.get("tool_name", "?"))

    @staticmethod
    def _permission_card(ev: dict, header: str = "🔐 <b>Permission needed</b>") -> str:
        tool = html.escape(ev.get("tool_name") or "a tool")
        summary = html.escape(ev.get("summary") or "")
        detail = ev.get("detail") or ""
        lines = [header, "", f"<b>{tool}</b>"]
        if summary:
            lines.append(summary)
        if detail:
            lines.append(f"<code>{html.escape(detail)}</code>")
        return "\n".join(lines)

    def handle_callback(self, query: dict, allowed_ids) -> bool:
        """Apply an inline-button press. Runs on the bridge's INBOUND thread.

        Returns True when the press was ours (handled), so the caller stops looking at it.
        Only allow-listed users are honoured — the same list that may drive the session at all.
        """
        data = (query.get("data") or "")
        if not data.startswith("p:"):
            return False
        parts = data.split(":")
        if len(parts) != 3:
            return True
        _, request_id, verdict = parts
        user_id = (query.get("from") or {}).get("id")
        if user_id not in set(allowed_ids or ()):
            self._answer_callback(query, "Not authorized.")
            log.warning("remote-control: rejected a permission press from an unknown user")
            return True

        with self._perm_lock:
            pending = self._pending_perms.pop(request_id, None)
        if pending is None:
            self._answer_callback(query, "That request is no longer waiting.")
            return True

        decision = "allow" if verdict == "a" else "deny"
        core.write_decision(self.bridge, request_id, decision, by=user_id)
        self._answer_callback(query, "Allowed ✅" if decision == "allow" else "Denied ⛔")
        message = (query.get("message") or {})
        text = message.get("text") or ""
        head = "✅ <b>Allowed</b>" if decision == "allow" else "⛔ <b>Denied</b>"
        self._edit_plain(pending["mid"], _replace_header(text, head),
                         parse_mode="HTML", reply_markup=NO_KEYBOARD)
        log.info("remote-control: permission %s → %s", request_id[:8], decision)
        return True

    def _answer_callback(self, query: dict, text: str) -> None:
        cb = self._answer_callback_query
        if cb is not None:
            cb(query.get("id", ""), text)

    def _resolve_permission(self, request_id: str, header: str) -> None:
        """Close out a card nobody pressed — expired, or answered at the keyboard instead."""
        with self._perm_lock:
            pending = self._pending_perms.pop(request_id, None)
        if pending is None:
            return
        self._edit_plain(pending["mid"], header, parse_mode="HTML", reply_markup=NO_KEYBOARD)

    def _resolve_all_permissions(self, header: str) -> None:
        with self._perm_lock:
            ids = list(self._pending_perms)
        for request_id in ids:
            self._resolve_permission(request_id, header)

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
        # The turn is over, so any card still showing buttons was answered at the keyboard
        # (or abandoned). Leaving live buttons would let a later press decide nothing.
        self._resolve_all_permissions("🖥️ <b>Answered at the terminal</b>")
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
        # Render the agent's Markdown, but never at the cost of losing content: if Telegram
        # rejects the rich version (or it would not fit), the same text goes out as plain.
        rendered = _render(text)
        if state.mid is None:
            mid = self._send_plain_id(rendered, parse_mode="HTML") if rendered else None
            if mid is None:
                mid = self._send_plain_id(text)
            if mid is None:
                return                             # send failed; retry on the next flush
            state.mid = mid
            # Metadata only — message CONTENT is never logged (see docs/SECURITY.md).
            log.info("MIRROR (stream) telegram msg %s, %d chars", mid, len(text))
        else:
            ok = self._edit_plain(state.mid, rendered, parse_mode="HTML") if rendered else False
            if not ok and not self._edit_plain(state.mid, text):
                return                             # leave `shown` alone so the next flush retries
        state.shown = text
        state.last_edit = time.monotonic()
        self._delivered = True
