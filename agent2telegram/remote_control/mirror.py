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

Text is rendered from the agent's Markdown to Telegram HTML, with a plain-text fallback on any
rejection, so formatting is never traded for content (see :func:`_render` and ``_write``).

State is **per Claude session**, not per bridge. One bridge can mirror several sessions at once,
and mixing their streams — or letting one session's turn end finalize another's half-written
message — is a correctness bug, not merely untidy output.
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
#: How long the roster of mirrored sessions is cached — it only decides message labelling.
ROSTER_TTL = 2.0
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
    """Swap the first line of a card for its outcome, keeping the details below.

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


class _Session:
    """Everything the mirror tracks for one Claude session."""

    __slots__ = ("live", "order", "delivered", "working", "waiting", "tag")

    def __init__(self) -> None:
        self.live: dict = {}     # message_id → _LiveMessage
        self.order: list = []
        self.delivered = False   # has this turn already put assistant text in the chat?
        self.working = False     # a turn is in flight → drive the typing indicator
        self.waiting: dict = {}  # open blocking dialogs, keyed by their id
        self.tag = ""            # short human label (the project directory), for multi-session


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
        self._sessions: dict = {}
        self._active = False
        self._last_event = 0.0
        self._roster: dict = {}
        self._roster_at = 0.0
        # Approval cards awaiting a button press. Touched by the bridge's outbound thread (when
        # a card is posted) AND by its inbound thread (when the press arrives), so it is locked.
        self._pending_perms: dict = {}
        self._perm_lock = threading.Lock()
        # Question cards awaiting an answer, same two-thread situation as the permission cards.
        self._pending_questions: dict = {}
        self._q_lock = threading.Lock()

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
            # No Stop/StopFailure ever arrived — end the turns ourselves rather than leave the
            # chat showing sessions that are still working.
            log.info("remote-control: no events for %.0fs, ending the mirrored turn(s)",
                     IDLE_DONE)
            for session_id in list(self._sessions):
                self._finalize_all(session_id)
                self._end_turn(session_id)
        self._flush()
        self._refresh_typing()
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

    # ---- per-session bookkeeping ------------------------------------------
    def _session(self, session_id: str) -> _Session:
        state = self._sessions.get(session_id)
        if state is None:
            state = self._sessions[session_id] = _Session()
        return state

    def _roster_size(self) -> int:
        """How many sessions this bridge mirrors (cached — it only decides labelling)."""
        now = time.monotonic()
        if now - self._roster_at > ROSTER_TTL:
            self._roster = core.sessions_for_bridge(self.bridge)
            self._roster_at = now
        return len(self._roster)

    def _prefix(self, session_id: str) -> str:
        """``"[project] "`` when more than one session is mirrored, else ``""``.

        A label on every line is noise in the normal one-session case, and its absence makes two
        interleaved sessions unreadable — so it appears exactly when it is needed.
        """
        if self._roster_size() < 2:
            return ""
        state = self._session(session_id)
        tag = state.tag or _basename(self._roster.get(session_id, {}).get("cwd", "")) \
            or (session_id[:6] if session_id else "session")
        return f"[{core.short(tag, 24)}] "

    def _refresh_typing(self) -> None:
        """Typing is on while any session is working and none of its dialogs are blocking."""
        want = any(s.working and not s.waiting for s in self._sessions.values())
        if want != self._active:
            self._active = want
            self._set_active(want)

    def _working(self, session_id: str, on: bool) -> None:
        self._session(session_id).working = on
        self._refresh_typing()

    # ---- event dispatch ----------------------------------------------------
    def _apply(self, ev: dict) -> None:
        kind = ev.get("type")
        sid = ev.get("session_id", "")
        if kind == "prompt":
            self._on_prompt(ev, sid)
        elif kind == "message":
            self._on_message(ev, sid)
        elif kind == "recap":
            self._on_recap(ev, sid)
        elif kind == "tool":
            self._working(sid, True)
            self._status_push(self._prefix(sid) + (ev.get("summary") or "🛠️ tool"))
        elif kind == "tool_failed":
            self._working(sid, True)
            summary = ev.get("summary") or "🛠️ tool"
            self._status_push(self._prefix(sid)
                              + "⚠️ Failed: " + core.short(summary.split(" ", 1)[-1], 50))
        elif kind == "subagent_start":
            self._working(sid, True)
            self._status_push(self._prefix(sid) + core.subagent_summary(ev.get("agent_type", "")))
        elif kind == "subagent_stop":
            self._status_push(self._prefix(sid) + "🤖 Subagent completed")
        elif kind == "task_created":
            self._status_push(self._prefix(sid)
                              + "📋 Working: " + core.short(ev.get("task_name", ""), 60))
        elif kind == "task_completed":
            self._status_push(self._prefix(sid)
                              + "✅ Task completed: " + core.short(ev.get("task_name", ""), 60))
        elif kind == "question":
            self._on_question(ev, sid)
        elif kind == "question_request":
            self._on_question_request(ev, sid)
        elif kind == "question_expired":
            self._resolve_question(ev.get("request_id", ""),
                                   "⌛ <b>Expired</b> — answer it at the terminal.")
        elif kind == "question_answered":
            self._resolve_dialog(sid, ev.get("tool_use_id", ""),
                                 "✅ <b>Answered at the terminal</b>")
        elif kind == "elicitation":
            self._on_elicitation(ev, sid)
        elif kind == "elicitation_done":
            self._resolve_dialog(sid, ev.get("elicitation_id", ""),
                                 "✅ <b>Answered at the terminal</b>")
        elif kind == "compact_start":
            self._working(sid, True)
            self._status_push(self._prefix(sid)
                              + f"🗜️ Compacting the conversation ({ev.get('trigger') or '?'})")
        elif kind == "compact_end":
            self._on_compact_end(ev, sid)
        elif kind == "permission":
            self._on_permission(ev, sid)
        elif kind == "permission_request":
            self._on_permission_request(ev, sid)
        elif kind == "permission_expired":
            self._resolve_permission(ev.get("request_id", ""),
                                     "⌛ <b>Expired</b> — answer it at the terminal.")
        elif kind == "notification":
            self._on_notification(ev, sid)
        elif kind == "interrupted":
            self._on_interrupted(ev, sid)
        elif kind == "turn_end":
            self._on_turn_end(ev, sid)
        elif kind == "turn_failed":
            self._on_turn_failed(ev, sid)
        elif kind == "session_end":
            self._on_session_end(ev, sid)

    # ---- prompts, recap, notices -------------------------------------------
    def _on_prompt(self, ev: dict, sid: str) -> None:
        text = (ev.get("text") or "").strip()
        state = self._session(sid)
        state.delivered = False
        if ev.get("cwd"):
            state.tag = _basename(ev["cwd"])
        self._working(sid, True)
        if text:
            # Plain text: a local prompt is arbitrary content and must never be re-parsed.
            self._send_text(self._prefix(sid) + "🖥️ You:\n" + text, parse_mode=None)

    def _on_recap(self, ev: dict, sid: str) -> None:
        """Connecting mid-session otherwise drops you into a stream with no context."""
        text = (ev.get("text") or "").strip()
        if ev.get("cwd"):
            self._session(sid).tag = _basename(ev["cwd"])
        if text:
            self._send_text("📜 Where this session is up to:\n\n" + text, parse_mode=None)

    def _on_notification(self, ev: dict, sid: str) -> None:
        texts = {
            "idle_prompt": "🔔 Waiting for your input.",
            "agent_needs_input": "🔔 Needs your input to continue.",
            "agent_completed": "🔔 Finished working.",
        }
        text = texts.get(ev.get("notification_type", ""))
        if text:
            self._send_text(self._prefix(sid) + text, parse_mode=None)

    def _on_compact_end(self, ev: dict, sid: str) -> None:
        self._status_clear()
        self._send_text(self._prefix(sid)
                        + f"🗜️ Conversation compacted ({ev.get('trigger') or '?'}). "
                          "Earlier context was summarized; the session continues.",
                        parse_mode=None)

    def _on_interrupted(self, ev: dict, sid: str) -> None:
        """Interrupted from Telegram — Claude Code sends no Stop or StopFailure for that.

        The bridge interrupts the tmux seat, not a particular Claude session, so an event with
        no session id ends every turn this bridge is mirroring."""
        targets = [sid] if sid else list(self._sessions)
        for target in targets:
            self._finalize_all(target)
            self._end_turn(target)

    # ---- blocking dialogs --------------------------------------------------
    def _on_question(self, ev: dict, sid: str) -> None:
        """Report a question the chat cannot answer — remote decisions are off, or no bridge was
        listening when it was asked.

        The answerable version is :meth:`_on_question_request`. Either way the session has
        STOPPED to ask and emits no turn end, so without this the chat would show "typing…"
        indefinitely beside a session that is really parked on a picker.
        """
        lines = ["❓ <b>Waiting for your answer</b>"]
        for q in ev.get("questions") or []:
            if not isinstance(q, dict):
                continue
            lines.append("")
            if q.get("header"):
                lines.append(f"<b>{html.escape(q['header'])}</b>")
            if q.get("question"):
                lines.append(html.escape(q["question"]))
            for i, option in enumerate(q.get("options") or [], 1):
                lines.append(f"  {i}. {html.escape(option)}")
        lines += ["", "<i>Remote answering is off for this session — answer it at the "
                      "terminal.</i>"]
        self._open_dialog(sid, ev.get("tool_use_id", ""), "\n".join(lines))

    def _on_question_request(self, ev: dict, sid: str) -> None:
        """Post an answerable question card; the PreToolUse hook is blocked waiting on it."""
        request_id = ev.get("request_id", "")
        questions = ev.get("questions") or []
        if not request_id or not questions:
            return
        self._status_clear()               # the question must be the last thing in the chat
        pending = {"questions": questions, "selections": {}, "session": sid,
                   "mid": None, "ts": time.monotonic()}
        mid = self._send_plain_id(self._question_card(sid, pending), parse_mode="HTML",
                                  reply_markup=self._question_keyboard(request_id, pending))
        if mid is None:
            log.warning("remote-control: could not post the question card; "
                        "the terminal picker will handle it")
            return
        pending["mid"] = mid
        with self._q_lock:
            self._pending_questions[request_id] = pending
        # Register it as a blocking dialog too, so "typing…" stops: the session is waiting.
        self._session(sid).waiting[f"q:{request_id}"] = {"mid": None}
        self._refresh_typing()
        log.info("MIRROR (question) card %s, %d question(s)", mid, len(questions))

    def _question_card(self, sid: str, pending: dict) -> str:
        lines = [f"{html.escape(self._prefix(sid))}❓ <b>Waiting for your answer</b>"]
        for qi, q in enumerate(pending["questions"]):
            chosen = pending["selections"].get(qi, [])
            lines.append("")
            if q.get("header"):
                lines.append(f"<b>{html.escape(q['header'])}</b>")
            if q.get("question"):
                lines.append(html.escape(q["question"]))
            for oi, option in enumerate(q.get("options") or []):
                mark = "✅ " if oi in chosen else "• "
                lines.append(f"  {mark}{html.escape(option)}")
        lines += ["", "<i>Tap an option, or reply to this message with your own answer.</i>"]
        return "\n".join(lines)

    @staticmethod
    def _question_keyboard(request_id: str, pending: dict) -> dict:
        rows = []
        for qi, q in enumerate(pending["questions"]):
            chosen = pending["selections"].get(qi, [])
            for oi, option in enumerate(q.get("options") or []):
                mark = "✅ " if oi in chosen else ""
                rows.append([{"text": mark + core.short(option, 30),
                              "callback_data": f"q:{request_id}:{qi}:{oi}"}])
        if not _auto_submits(pending["questions"]):
            rows.append([{"text": "📨 Send answer", "callback_data": f"q:{request_id}:s:s"}])
        return {"inline_keyboard": rows}

    def _handle_question_press(self, query: dict, request_id: str, qi: str, oi: str) -> None:
        with self._q_lock:
            pending = self._pending_questions.get(request_id)
        if pending is None:
            self._answer_callback(query, "That question is no longer waiting.")
            return
        if qi == "s":
            self._submit_question(query, request_id, pending)
            return
        try:
            qi_i, oi_i = int(qi), int(oi)
            question = pending["questions"][qi_i]
        except (ValueError, IndexError):
            return
        chosen = pending["selections"].setdefault(qi_i, [])
        if question.get("multi"):
            chosen.remove(oi_i) if oi_i in chosen else chosen.append(oi_i)
        else:
            pending["selections"][qi_i] = [oi_i]
        if _auto_submits(pending["questions"]):
            self._submit_question(query, request_id, pending)
            return
        # Reflect the selection and keep waiting for the rest.
        self._answer_callback(query, "Selected")
        self._edit_plain(pending["mid"],
                         self._question_card(pending["session"], pending), parse_mode="HTML",
                         reply_markup=self._question_keyboard(request_id, pending))

    def _submit_question(self, query, request_id: str, pending: dict) -> None:
        answer = _format_answer(pending)
        if not answer:
            self._answer_callback(query, "Choose an option first.")
            return
        self._deliver_answer(request_id, pending, answer)
        if query is not None:
            self._answer_callback(query, "Sent ✅")

    def _deliver_answer(self, request_id: str, pending: dict, answer: str) -> None:
        """Hand the answer to the waiting hook and close the card."""
        with self._q_lock:
            self._pending_questions.pop(request_id, None)
        core.write_decision(self.bridge, request_id, "answer", by=None, answer=answer)
        self._edit_plain(pending["mid"],
                         "✅ <b>Answered</b>\n\n" + html.escape(answer),
                         parse_mode="HTML", reply_markup=NO_KEYBOARD)
        sid = pending.get("session", "")
        self._session(sid).waiting.pop(f"q:{request_id}", None)
        self._refresh_typing()
        log.info("remote-control: question %s answered from Telegram", request_id[:8])

    def answer_question_reply(self, reply_to_mid: int, text: str) -> bool:
        """A free-text reply to a question card is the answer. Runs on the INBOUND thread.

        Returns True when it was consumed, so the bridge does not also inject it as a prompt.
        Authorization is the caller's job — it already checked the allow-list."""
        text = (text or "").strip()
        if not text:
            return False
        with self._q_lock:
            match = next(((rid, p) for rid, p in self._pending_questions.items()
                          if p.get("mid") == reply_to_mid), None)
        if match is None:
            return False
        request_id, pending = match
        self._deliver_answer(request_id, pending, text)
        return True

    def _resolve_question(self, request_id: str, header: str) -> None:
        with self._q_lock:
            pending = self._pending_questions.pop(request_id, None)
        if pending is None:
            return
        self._edit_plain(pending["mid"], header, parse_mode="HTML", reply_markup=NO_KEYBOARD)
        self._session(pending.get("session", "")).waiting.pop(f"q:{request_id}", None)
        self._refresh_typing()

    def _resolve_session_questions(self, sid: str, header: str) -> None:
        with self._q_lock:
            ids = [rid for rid, p in self._pending_questions.items() if p.get("session") == sid]
        for request_id in ids:
            self._resolve_question(request_id, header)

    def _on_elicitation(self, ev: dict, sid: str) -> None:
        server = ev.get("server_name") or "an MCP server"
        body = [f"❓ <b>{html.escape(server)} is asking for input</b>"]
        if ev.get("prompt"):
            body += ["", html.escape(ev["prompt"])]
        body += ["", "<i>Answer it at the terminal.</i>"]
        self._open_dialog(sid, ev.get("elicitation_id", ""), "\n".join(body))

    def _open_dialog(self, sid: str, key: str, text: str) -> None:
        state = self._session(sid)
        key = key or "dialog"
        if key in state.waiting:
            return
        self._status_clear()               # the question must be the last thing in the chat
        mid = self._send_plain_id(self._prefix(sid) + text, parse_mode="HTML")
        state.waiting[key] = {"mid": mid}
        self._refresh_typing()             # stop "typing…": it is waiting, not working
        log.info("MIRROR (dialog) session blocked on a human answer")

    def _resolve_dialog(self, sid: str, key: str, header: str) -> None:
        state = self._session(sid)
        pending = state.waiting.pop(key or "dialog", None)
        if pending is None:
            return
        if pending.get("mid") is not None:
            self._edit_plain(pending["mid"], header, parse_mode="HTML")
        self._refresh_typing()

    def _resolve_all_dialogs(self, sid: str, header: str) -> None:
        for key in list(self._session(sid).waiting):
            self._resolve_dialog(sid, key, header)

    # ---- permissions -------------------------------------------------------
    def _on_permission(self, ev: dict, sid: str) -> None:
        self._send_text(
            self._prefix(sid) + "🔐 Waiting for permission\n\n"
            f"{ev.get('summary') or ev.get('tool_name') or 'a tool call'}\n\n"
            "Approve or deny it in the terminal — remote approval is off for this session.",
            parse_mode=None)

    def _on_permission_request(self, ev: dict, sid: str) -> None:
        """Post an approval card with Allow/Deny buttons; the hook is blocked waiting on it."""
        request_id = ev.get("request_id", "")
        if not request_id:
            return
        self._working(sid, True)
        self._status_clear()                       # the card must be the last thing in the chat
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Allow", "callback_data": f"p:{request_id}:a"},
            {"text": "⛔ Deny", "callback_data": f"p:{request_id}:d"},
        ]]}
        mid = self._send_plain_id(self._permission_card(ev, self._prefix(sid)),
                                  parse_mode="HTML", reply_markup=keyboard)
        if mid is None:
            log.warning("remote-control: could not post the approval card; "
                        "the terminal prompt will handle it")
            return
        with self._perm_lock:
            self._pending_perms[request_id] = {"mid": mid, "ts": time.monotonic(),
                                               "session": sid}
        log.info("MIRROR (permission) card %s for %s", mid, ev.get("tool_name", "?"))

    @staticmethod
    def _permission_card(ev: dict, prefix: str = "") -> str:
        tool = html.escape(ev.get("tool_name") or "a tool")
        summary = html.escape(ev.get("summary") or "")
        detail = ev.get("detail") or ""
        lines = [f"{html.escape(prefix)}🔐 <b>Permission needed</b>", "", f"<b>{tool}</b>"]
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
        if data.startswith("q:"):
            parts = data.split(":")
            if len(parts) == 4 and (query.get("from") or {}).get("id") in set(allowed_ids or ()):
                self._handle_question_press(query, parts[1], parts[2], parts[3])
            elif len(parts) == 4:
                self._answer_callback(query, "Not authorized.")
                log.warning("remote-control: rejected a question press from an unknown user")
            return True
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
        text = (query.get("message") or {}).get("text") or ""
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

    def _resolve_session_permissions(self, sid: str, header: str) -> None:
        with self._perm_lock:
            ids = [rid for rid, p in self._pending_perms.items() if p.get("session") == sid]
        for request_id in ids:
            self._resolve_permission(request_id, header)

    # ---- turn end ----------------------------------------------------------
    def _on_turn_end(self, ev: dict, sid: str) -> None:
        self._finalize_all(sid)
        # Backstop: if nothing reached the chat through MessageDisplay (hook not registered,
        # a mirror error, an answer rendered some other way), deliver the documented final
        # message. No transcript parsing, and never a second copy of streamed text.
        if not self._session(sid).delivered:
            answer = (ev.get("last_assistant_message") or "").strip()
            if answer:
                self._send_text(self._prefix(sid) + "🖥️ " + answer)
                log.info("remote-control: Stop backstop delivered the final answer")
        self._end_turn(sid)

    def _on_turn_failed(self, ev: dict, sid: str) -> None:
        self._finalize_all(sid)
        error = (ev.get("error") or "unknown error").strip()
        partial = (ev.get("partial") or "").strip()
        body = self._prefix(sid) + f"⚠️ Turn ended with an error\n\n{error}"
        if partial and not self._session(sid).delivered:
            # Nothing was streamed, but Claude Code handed us what it had — better than nothing.
            body += f"\n\nLast thing it said:\n{partial}"
        self._send_text(body, parse_mode=None)
        self._end_turn(sid)

    def _on_session_end(self, ev: dict, sid: str) -> None:
        self._finalize_all(sid)
        self._end_turn(sid)
        self._sessions.pop(sid, None)
        self._refresh_typing()

    def _end_turn(self, sid: str) -> None:
        # The turn is over, so any card still showing buttons was answered at the keyboard
        # (or abandoned). Leaving live buttons would let a later press decide nothing.
        self._resolve_session_permissions(sid, "🖥️ <b>Answered at the terminal</b>")
        self._resolve_session_questions(sid, "🖥️ <b>Answered at the terminal</b>")
        self._resolve_all_dialogs(sid, "🖥️ <b>Answered at the terminal</b>")
        self._status_clear()
        state = self._session(sid)
        state.delivered = False
        state.working = False
        self._refresh_typing()

    # ---- streaming ---------------------------------------------------------
    def _on_message(self, ev: dict, sid: str) -> None:
        state = self._session(sid)
        mid = ev.get("message_id") or ev.get("turn_id") or "message"
        live = state.live.get(mid)
        if live is None:
            live = state.live[mid] = _LiveMessage()
            state.order.append(mid)
            # A new assistant message means the trailing tool bubble must move below it.
            self._status_clear()
        self._working(sid, True)
        live.buf += ev.get("delta") or ""
        if ev.get("final"):
            live.closed = True
            self._emit(live, sid, force=True)      # an empty final delta still finalizes
            self._forget(state, mid)

    @staticmethod
    def _forget(state: _Session, mid: str) -> None:
        state.live.pop(mid, None)
        if mid in state.order:
            state.order.remove(mid)

    def _flush(self) -> None:
        """Apply throttled edits for every message still streaming, in every session."""
        now = time.monotonic()
        for sid, state in list(self._sessions.items()):
            for mid in list(state.order):
                live = state.live.get(mid)
                if live is None:
                    continue
                if now - live.last_edit >= EDIT_INTERVAL or len(live.buf) > CHUNK_LIMIT:
                    self._emit(live, sid)

    def _finalize_all(self, sid: str) -> None:
        state = self._session(sid)
        for mid in list(state.order):
            live = state.live.get(mid)
            if live is not None:
                live.closed = True
                self._emit(live, sid, force=True)
            self._forget(state, mid)

    def _emit(self, live: _LiveMessage, sid: str, force: bool = False) -> None:
        """Push the buffer to Telegram, continuing into new messages when it outgrows one."""
        if not force and time.monotonic() - live.last_edit < EDIT_INTERVAL:
            return
        while len(live.buf) > CHUNK_LIMIT:
            head, tail = _split_head(live.buf, CHUNK_LIMIT)
            live.buf = head
            self._write(live, sid)                # close the current Telegram message on `head`
            live.buf, live.mid, live.shown = tail, None, ""
        self._write(live, sid)

    def _write(self, live: _LiveMessage, sid: str) -> None:
        text = live.buf
        if not text.strip():
            return                                 # nothing worth a Telegram message yet
        if text == live.shown:
            live.last_edit = time.monotonic()
            return
        body = self._prefix(sid) + text
        # Render the agent's Markdown, but never at the cost of losing content: if Telegram
        # rejects the rich version (or it would not fit), the same text goes out as plain.
        rendered = _render(body)
        if live.mid is None:
            mid = self._send_plain_id(rendered, parse_mode="HTML") if rendered else None
            if mid is None:
                mid = self._send_plain_id(body)
            if mid is None:
                return                             # send failed; retry on the next flush
            live.mid = mid
            # Metadata only — message CONTENT is never logged (see docs/SECURITY.md).
            log.info("MIRROR (stream) telegram msg %s, %d chars", mid, len(text))
        else:
            ok = self._edit_plain(live.mid, rendered, parse_mode="HTML") if rendered else False
            if not ok and not self._edit_plain(live.mid, body):
                return                             # leave `shown` alone so the next flush retries
        live.shown = text
        live.last_edit = time.monotonic()
        self._session(sid).delivered = True


def _auto_submits(questions: list) -> bool:
    """One single-select question answers itself on the first tap — the common case."""
    return len(questions) == 1 and not questions[0].get("multi")


def _format_answer(pending: dict) -> str:
    """The user's selections, phrased for the model that will read them."""
    parts = []
    for qi, q in enumerate(pending["questions"]):
        chosen = pending["selections"].get(qi) or []
        options = q.get("options") or []
        labels = [options[i] for i in chosen if 0 <= i < len(options)]
        if not labels:
            continue
        topic = q.get("header") or q.get("question") or f"question {qi + 1}"
        parts.append(f"{topic}: {', '.join(labels)}")
    return "; ".join(parts)


def _basename(path: str) -> str:
    return (path or "").rstrip("/").rsplit("/", 1)[-1]
