"""Tests for local Remote Control: state, hook classification, spool and the Telegram mirror.

Standard library only (``python -m unittest discover -s tests``), no network, no tmux and no
real Claude Code session — the hook side is pure filesystem work and the mirror side takes its
Telegram operations as callables, so both are directly observable.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent2telegram.remote_control import core
from agent2telegram.remote_control import mirror as mirror_mod
from agent2telegram.remote_control.mirror import RemoteControlMirror
from agent2telegram.telegram import markdown_to_html


def _mirror_for(chat):
    """A mirror wired to a fake chat — the same wiring AttachBridge does."""
    return RemoteControlMirror(
        BRIDGE, send_plain_id=chat.send_plain_id, edit_plain=chat.edit_plain,
        send_text=chat.send_text, status_push=chat.status_push,
        status_clear=chat.status_clear, set_active=chat.set_active,
        answer_callback_query=chat.answer_callback_query)


def rendered(*texts):
    """What the mirror is expected to put on the wire: the agent's Markdown as Telegram HTML."""
    return [markdown_to_html(t) for t in texts]

BRIDGE = "test-seat"
SID = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


class _StateCase(unittest.TestCase):
    """Every test gets its own state root, so nothing touches the real machine."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("AGENT2TELEGRAM_STATE")
        os.environ["AGENT2TELEGRAM_STATE"] = self._tmp.name
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._prev is None:
            os.environ.pop("AGENT2TELEGRAM_STATE", None)
        else:
            os.environ["AGENT2TELEGRAM_STATE"] = self._prev
        self._tmp.cleanup()

    def enable(self, session_id: str = SID, origins=("[TG] ",)) -> None:
        core.bind_session(session_id, bridge=BRIDGE, config_path="/dev/null",
                          origins=origins, label="Test")

    def hook(self, event: str, **fields) -> None:
        self.hook_out(event, **fields)

    def hook_out(self, event: str, **fields):
        """Run one hook payload and return whatever JSON output it produced (usually None)."""
        return core.handle({"hook_event_name": event,
                            "session_id": fields.pop("session_id", SID), **fields})

    def events(self):
        return [e for _, e in core.read_events(BRIDGE) if e]

    def kinds(self):
        return [e["type"] for e in self.events()]


# --------------------------------------------------------------------------- state

class RemoteStateTests(_StateCase):
    def test_enable_creates_marker_and_index(self):
        self.enable()
        self.assertTrue(os.path.exists(core.enabled_marker(BRIDGE, SID)))
        self.assertTrue(os.path.exists(core.session_index(SID)))
        self.assertTrue(core.is_enabled(SID))

    def test_disable_removes_marker(self):
        self.enable()
        self.assertTrue(core.unbind_session(SID))
        self.assertFalse(core.is_enabled(SID))
        self.assertFalse(os.path.exists(core.enabled_marker(BRIDGE, SID)))
        self.assertFalse(os.path.exists(core.session_index(SID)))

    def test_disable_is_idempotent(self):
        self.assertFalse(core.unbind_session(SID))

    def test_sessions_are_isolated(self):
        self.enable(SID)
        self.assertTrue(core.is_enabled(SID))
        self.assertFalse(core.is_enabled(OTHER))
        self.enable(OTHER)
        core.unbind_session(SID)
        self.assertFalse(core.is_enabled(SID))
        self.assertTrue(core.is_enabled(OTHER))

    def test_binding_without_marker_reads_as_disabled(self):
        self.enable()
        os.unlink(core.enabled_marker(BRIDGE, SID))
        self.assertIsNone(core.session_binding(SID))

    def test_state_files_are_private(self):
        self.enable()
        core.set_origin(BRIDGE, SID, "terminal")
        core.write_event(BRIDGE, {"type": "prompt", "text": "hi"})
        for path in (core.enabled_marker(BRIDGE, SID), core.session_index(SID),
                     core.origin_path(BRIDGE, SID),
                     *(p for p, _ in core.read_events(BRIDGE))):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, path)
        self.assertEqual(os.stat(core.events_dir(BRIDGE)).st_mode & 0o777, 0o700)
        core.touch_heartbeat(BRIDGE)
        self.assertEqual(os.stat(core.heartbeat_path(BRIDGE)).st_mode & 0o777, 0o600)
        # Every directory level, not just the leaf — os.makedirs only modes the last one.
        for d in (core.root_dir(), core.sessions_dir(), core.bridge_dir(BRIDGE),
                  os.path.dirname(core.enabled_marker(BRIDGE, SID)),
                  os.path.dirname(core.origin_path(BRIDGE, SID))):
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o700, d)


# --------------------------------------------------------------------------- lifecycle

class SessionLifecycleTests(_StateCase):
    def _session_start(self, source: str) -> None:
        self.hook("SessionStart", source=source)

    def test_startup_disables(self):
        self.enable()
        self._session_start("startup")
        self.assertFalse(core.is_enabled(SID))

    def test_resume_disables(self):
        self.enable()
        self._session_start("resume")
        self.assertFalse(core.is_enabled(SID))

    def test_fork_disables(self):
        self.enable()
        self._session_start("fork")
        self.assertFalse(core.is_enabled(SID))

    def test_compact_preserves(self):
        self.enable()
        self._session_start("compact")
        self.assertTrue(core.is_enabled(SID))

    def test_clear_preserves(self):
        self.enable()
        self._session_start("clear")
        self.assertTrue(core.is_enabled(SID))

    def test_session_end_clear_does_not_disconnect(self):
        self.enable()
        self.hook("SessionEnd", reason="clear")
        self.assertTrue(core.is_enabled(SID))
        self.assertEqual(self.kinds(), [])

    def test_session_end_resume_does_not_disconnect(self):
        self.enable()
        self.hook("SessionEnd", reason="resume")
        self.assertTrue(core.is_enabled(SID))

    def test_true_exit_cleans_state(self):
        self.enable()
        self.hook("SessionEnd", reason="prompt_input_exit")
        self.assertFalse(core.is_enabled(SID))
        self.assertEqual(self.kinds(), ["session_end"])

    def test_logout_cleans_state(self):
        self.enable()
        self.hook("SessionEnd", reason="logout")
        self.assertFalse(core.is_enabled(SID))

    def test_session_start_on_disabled_session_is_harmless(self):
        self._session_start("startup")
        self.assertFalse(core.is_enabled(SID))
        self.assertEqual(self.kinds(), [])


# --------------------------------------------------------------------------- origin

class OriginTests(_StateCase):
    def test_local_prompt_is_terminal(self):
        self.assertEqual(core.classify_origin("fix the importer", ("[TG] ",)), "terminal")

    def test_bracket_tg_prefix_is_telegram(self):
        self.assertEqual(core.classify_origin("[TG] fix it", ()), "telegram")

    def test_legacy_telegram_prefix_is_telegram(self):
        self.assertEqual(core.classify_origin("Telegram: fix it", ()), "telegram")

    def test_configured_prefix_is_telegram(self):
        self.assertEqual(core.classify_origin(">>tg<< fix it", (">>tg<<",)), "telegram")

    def test_terminal_prompt_is_mirrored(self):
        self.enable()
        self.hook("UserPromptSubmit", user_input="investigate duplicates")
        self.assertEqual(self.kinds(), ["prompt"])
        self.assertEqual(self.events()[0]["text"], "investigate duplicates")
        self.assertEqual(core.get_origin(BRIDGE, SID), "terminal")

    def test_telegram_prompt_is_not_mirrored(self):
        self.enable()
        self.hook("UserPromptSubmit", user_input="[TG] investigate duplicates")
        self.assertEqual(self.kinds(), [])
        self.assertEqual(core.get_origin(BRIDGE, SID), "telegram")

    def test_telegram_turn_produces_no_mirror_events_at_all(self):
        self.enable()
        self.hook("UserPromptSubmit", user_input="[TG] do the thing")
        self.hook("MessageDisplay", message_id="m1", delta="working…", final=False)
        self.hook("PreToolUse", tool_name="Read", tool_input={"file_path": "/a/b.ts"})
        self.hook("Stop", last_assistant_message="done")
        self.assertEqual(self.kinds(), [])

    def test_next_terminal_turn_reclassifies(self):
        self.enable()
        self.hook("UserPromptSubmit", user_input="[TG] remote turn")
        self.hook("UserPromptSubmit", user_input="local turn")
        self.assertEqual(core.get_origin(BRIDGE, SID), "terminal")
        self.assertEqual(self.kinds(), ["prompt"])


# --------------------------------------------------------------------------- disabled

class DisabledTests(_StateCase):
    def test_terminal_turn_generates_no_traffic_when_disabled(self):
        for event, fields in (("UserPromptSubmit", {"user_input": "hello"}),
                              ("MessageDisplay", {"message_id": "m", "delta": "x"}),
                              ("PreToolUse", {"tool_name": "Bash", "tool_input": {}}),
                              ("Stop", {"last_assistant_message": "done"}),
                              ("StopFailure", {"error_type": "overloaded"})):
            self.hook(event, **fields)
        self.assertEqual(core.pending_count(BRIDGE), 0)

    def test_hook_never_raises_and_says_nothing(self):
        # No output at all means Claude Code behaves exactly as if the hook weren't there.
        self.assertIsNone(core.handle({}))
        self.assertIsNone(core.handle({"hook_event_name": "MessageDisplay"}))
        self.assertIsNone(core.handle({"hook_event_name": "Nonsense", "session_id": SID}))

    def test_hook_main_writes_nothing_for_ordinary_events(self):
        out = io.StringIO()
        self.assertEqual(core.hook_main(io.StringIO('{"hook_event_name":"Stop"}'), out), 0)
        self.assertEqual(out.getvalue(), "")

    def test_hook_main_survives_malformed_stdin(self):
        out = io.StringIO()
        self.assertEqual(core.hook_main(io.StringIO("not json"), out), 0)
        self.assertEqual(out.getvalue(), "")


# --------------------------------------------------------------------------- summaries

class SummaryTests(unittest.TestCase):
    def test_known_tools(self):
        self.assertEqual(core.tool_summary("Read", {"file_path": "/x/transactions.ts"}),
                         "📄 Reading transactions.ts")
        self.assertEqual(core.tool_summary("Edit", {"file_path": "/x/importer.ts"}),
                         "✏️ Editing importer.ts")
        self.assertEqual(core.tool_summary("Grep", {"pattern": "pendingTransaction"}),
                         "🔎 Searching pendingTransaction")
        self.assertEqual(core.tool_summary("WebFetch", {"url": "https://example.com/a"}),
                         "🌐 Web example.com")
        self.assertEqual(core.tool_summary("mcp__vercel__get_deployment", {}),
                         "🔌 vercel get_deployment")

    def test_bash_prefers_description_over_command(self):
        self.assertEqual(core.tool_summary("Bash", {"description": "Run tests",
                                                    "command": "pnpm test"}),
                         "🛠️ Run tests")

    def test_secrets_are_redacted_not_dumped(self):
        out = core.tool_summary("Bash", {"command": "curl -H 'Bearer abcdef0123456789xyz' u"})
        self.assertNotIn("abcdef0123456789xyz", out)
        out = core.tool_summary("Bash", {"command": "export API_TOKEN=s3cr3t-value-here"})
        self.assertNotIn("s3cr3t-value-here", out)
        out = core.tool_summary("Bash", {"command": "gh auth login --with-token ghp_" + "a" * 36})
        self.assertNotIn("ghp_" + "a" * 36, out)

    def test_summary_is_single_line_and_bounded(self):
        out = core.tool_summary("Bash", {"description": "x" * 500})
        self.assertNotIn("\n", out)
        self.assertLessEqual(len(out), 80)


# --------------------------------------------------------------------------- mirror

class _FakeChat:
    """Records the Telegram operations the mirror asks for."""

    def __init__(self) -> None:
        self.messages: dict[int, str] = {}
        self.order: list[int] = []
        self.sent_text: list[str] = []
        self.status: list[str] = []
        self.active: list[bool] = []
        self.keyboards: dict[int, dict] = {}
        self.parse_modes: list = []
        self.answered: list = []
        self.cleared = 0
        self._next = 100
        self.fail_send = False
        self.reject_html = False        # simulate Telegram refusing a parse_mode=HTML payload

    # streaming primitives
    def send_plain_id(self, text: str, parse_mode=None, reply_markup=None):
        if self.fail_send:
            return None
        if self.reject_html and parse_mode == "HTML":
            return None
        self._next += 1
        self.messages[self._next] = text
        self.order.append(self._next)
        self.parse_modes.append(parse_mode)
        if reply_markup is not None:
            self.keyboards[self._next] = reply_markup
        return self._next

    def edit_plain(self, mid: int, text: str, parse_mode=None, reply_markup=None) -> bool:
        if self.reject_html and parse_mode == "HTML":
            return False
        self.messages[mid] = text
        self.parse_modes.append(parse_mode)
        if reply_markup is not None:
            self.keyboards[mid] = reply_markup
        return True

    def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        self.answered.append((callback_id, text))

    # durable send / bubble / typing
    def send_text(self, text: str, parse_mode: str = "auto") -> None:
        self.sent_text.append(text)

    def status_push(self, line: str) -> None:
        self.status.append(line)

    def status_clear(self) -> None:
        self.cleared += 1

    def set_active(self, on: bool) -> None:
        self.active.append(on)

    def stream(self) -> list[str]:
        return [self.messages[m] for m in self.order]


class MirrorTests(_StateCase):
    def setUp(self) -> None:
        super().setUp()
        self.chat = _FakeChat()
        self.mirror = _mirror_for(self.chat)
        self._interval = mirror_mod.EDIT_INTERVAL
        mirror_mod.EDIT_INTERVAL = 0.0            # deterministic: never defer an edit
        self.addCleanup(self._restore_interval)
        self.enable()

    def _restore_interval(self) -> None:
        mirror_mod.EDIT_INTERVAL = self._interval

    def display(self, mid: str, delta: str, final: bool = False, **kw) -> None:
        self.hook("MessageDisplay", message_id=mid, turn_id="t1", index=0,
                  delta=delta, final=final, **kw)

    # ---- streaming
    def test_first_delta_creates_a_message(self):
        self.display("m1", "I'll inspect the importer.")
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), rendered("I'll inspect the importer."))

    def test_later_deltas_edit_the_same_message(self):
        self.display("m1", "Hello")
        self.mirror.tick()
        self.display("m1", " world")
        self.mirror.tick()
        self.assertEqual(len(self.chat.order), 1)
        self.assertEqual(self.chat.stream(), ["Hello world"])

    def test_final_finalizes(self):
        self.display("m1", "Answer", final=True)
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), ["Answer"])
        self.assertEqual(self.mirror._session(SID).live, {})

    def test_empty_final_delta_still_finalizes(self):
        self.display("m1", "Partial")
        self.mirror.tick()
        self.display("m1", "", final=True)
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), ["Partial"])
        self.assertEqual(self.mirror._session(SID).live, {})

    def test_multiple_message_ids_stay_independent(self):
        self.display("m1", "first")
        self.display("m2", "second")
        self.mirror.tick()
        self.display("m1", "-more", final=True)
        self.display("m2", "-also", final=True)
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), ["first-more", "second-also"])

    def test_throttling_never_loses_text(self):
        mirror_mod.EDIT_INTERVAL = 1e6            # no interim edit can possibly fire
        for part in ("alpha ", "beta ", "gamma"):
            self.display("m1", part)
            self.mirror.tick()
        self.display("m1", "", final=True)
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), ["alpha beta gamma"])

    def test_oversized_message_continues_without_truncation(self):
        words = [f"w{i}" for i in range(4000)]
        text = " ".join(words)
        self.assertGreater(len(text), mirror_mod.CHUNK_LIMIT)
        self.display("m1", text, final=True)
        self.mirror.tick()
        chunks = self.chat.stream()
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), mirror_mod.CHUNK_LIMIT)
        self.assertEqual(" ".join(chunks).split(), words)

    def test_send_failure_is_retried_on_the_next_flush(self):
        self.chat.fail_send = True
        self.display("m1", "will fail first")
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), [])
        self.chat.fail_send = False
        self.display("m1", " then land", final=True)
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), ["will fail first then land"])

    # ---- prompts / tools
    def test_local_prompt_is_mirrored_as_plain_text(self):
        self.hook("UserPromptSubmit", user_input="why are pending txns duplicated?")
        self.mirror.tick()
        self.assertEqual(self.chat.sent_text, ["🖥️ You:\nwhy are pending txns duplicated?"])

    def test_tool_updates_one_temporary_bubble(self):
        self.hook("PreToolUse", tool_name="Read", tool_input={"file_path": "/x/a.ts"})
        self.hook("PreToolUse", tool_name="Edit", tool_input={"file_path": "/x/b.ts"})
        self.mirror.tick()
        self.assertEqual(self.chat.status, ["📄 Reading a.ts", "✏️ Editing b.ts"])
        self.assertEqual(len(self.chat.order), 0)      # no durable message per tool call

    def test_new_assistant_message_repositions_the_bubble(self):
        self.hook("PreToolUse", tool_name="Read", tool_input={"file_path": "/x/a.ts"})
        self.mirror.tick()
        before = self.chat.cleared
        self.display("m1", "Now editing.")
        self.mirror.tick()
        self.assertGreater(self.chat.cleared, before)

    def test_turn_end_clears_the_bubble_and_typing(self):
        self.hook("PreToolUse", tool_name="Read", tool_input={"file_path": "/x/a.ts"})
        self.display("m1", "Done.", final=True)
        self.hook("Stop", last_assistant_message="Done.")
        self.mirror.tick()
        self.assertGreater(self.chat.cleared, 0)
        self.assertEqual(self.chat.active[-1], False)

    def test_tool_failure_is_surfaced(self):
        self.hook("PostToolUseFailure", tool_name="Bash",
                  tool_input={"description": "Run tests"}, error="exit 1")
        self.mirror.tick()
        self.assertTrue(self.chat.status[-1].startswith("⚠️ Failed:"))

    def test_subagent_and_task_events_use_the_bubble(self):
        self.hook("SubagentStart", agent_type="Explore")
        self.hook("TaskCreated", task_id="1", task_name="reconcile pending transactions")
        self.hook("TaskCompleted", task_id="1", task_name="reconcile pending transactions")
        self.hook("SubagentStop", agent_type="Explore")
        self.mirror.tick()
        self.assertEqual(self.chat.status, [
            "🤖 Explore running",
            "📋 Working: reconcile pending transactions",
            "✅ Task completed: reconcile pending transactions",
            "🤖 Subagent completed",
        ])
        self.assertEqual(len(self.chat.order), 0)

    def test_permission_notify_only_mode_never_offers_buttons(self):
        core.bind_session(SID, bridge=BRIDGE, permissions=False)
        self.assertIsNone(self.hook_out("PermissionRequest", tool_name="Bash",
                                        tool_input={"description": "delete things"}))
        self.mirror.tick()
        self.assertEqual(len(self.chat.sent_text), 1)
        self.assertIn("Waiting for permission", self.chat.sent_text[0])
        self.assertIn("terminal", self.chat.sent_text[0])
        self.assertEqual(self.chat.keyboards, {})

    def test_notifications_are_filtered(self):
        self.hook("Notification", notification_type="permission_prompt")
        self.hook("Notification", notification_type="idle_prompt")
        self.mirror.tick()
        self.assertEqual(len(self.chat.sent_text), 1)   # permission_prompt is PermissionRequest's

    # ---- Stop / StopFailure
    def test_stop_does_not_resend_streamed_text(self):
        self.display("m1", "The importer is fine.", final=True)
        self.hook("Stop", last_assistant_message="The importer is fine.")
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), ["The importer is fine."])
        self.assertEqual(self.chat.sent_text, [])       # no "finished" duplicate

    def test_stop_backstop_fires_only_when_nothing_was_streamed(self):
        self.hook("Stop", last_assistant_message="Only answer")
        self.mirror.tick()
        self.assertEqual(self.chat.sent_text, ["🖥️ Only answer"])
        self.assertEqual(self.chat.stream(), [])

    def test_stop_backstop_does_not_fire_twice(self):
        self.hook("Stop", last_assistant_message="Only answer")
        self.mirror.tick()
        self.hook("Stop", last_assistant_message="Only answer")
        self.mirror.tick()
        self.assertEqual(len(self.chat.sent_text), 2)   # two real turns → two answers
        self.display("m2", "streamed", final=True)
        self.hook("Stop", last_assistant_message="streamed")
        self.mirror.tick()
        self.assertEqual(len(self.chat.sent_text), 2)   # streamed turn adds nothing

    def test_stop_finalizes_a_message_left_open(self):
        self.display("m1", "half written")
        self.hook("Stop", last_assistant_message="half written")
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), ["half written"])
        self.assertEqual(self.chat.sent_text, [])

    def test_idle_turn_is_ended_so_typing_cannot_hang(self):
        self.display("m1", "working…")
        self.mirror.tick()
        self.assertEqual(self.chat.active[-1], True)
        self.mirror._last_event -= mirror_mod.IDLE_DONE + 1     # pretend a long silence
        self.mirror.tick()
        self.assertEqual(self.chat.active[-1], False)
        self.assertEqual(self.chat.stream(), ["working…"])       # partial text is kept
        self.assertEqual(self.mirror._session(SID).live, {})

    def test_stop_failure_ends_the_turn_without_stop(self):
        self.display("m1", "started…")
        self.hook("StopFailure", error_type="overloaded", error_message="upstream is busy")
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), ["started…"])
        self.assertEqual(len(self.chat.sent_text), 1)
        self.assertIn("overloaded", self.chat.sent_text[0])
        self.assertIn("upstream is busy", self.chat.sent_text[0])
        self.assertGreater(self.chat.cleared, 0)
        self.assertEqual(self.chat.active[-1], False)
        self.assertEqual(self.mirror._session(SID).live, {})


# --------------------------------------------------------------------------- permissions

class PermissionApprovalTests(_StateCase):
    """Allow/Deny travelling out to Telegram and the decision travelling back."""

    def setUp(self) -> None:
        super().setUp()
        self.chat = _FakeChat()
        self.mirror = _mirror_for(self.chat)
        self.enable()
        core.touch_heartbeat(BRIDGE)          # a bridge is listening

    def _request(self, **kw):
        """Run the (blocking) PermissionRequest hook on a thread; return a handle to its result."""
        box = {}

        def run():
            box["out"] = core.handle({
                "hook_event_name": "PermissionRequest", "session_id": SID,
                "tool_name": kw.get("tool_name", "Bash"),
                "tool_input": kw.get("tool_input", {"command": "rm -rf ./build"}),
                "tool_use_id": "tu1"})

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, box

    def _await_card(self, timeout=5.0):
        """Drain the spool until the approval card has been posted."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.mirror.tick()
            if self.chat.keyboards:
                return next(iter(self.chat.keyboards))
            time.sleep(0.02)
        self.fail("no approval card was posted")

    @staticmethod
    def _callback_data(keyboard, verdict):
        for row in keyboard["inline_keyboard"]:
            for button in row:
                if button["callback_data"].endswith(":" + verdict):
                    return button["callback_data"]
        raise AssertionError("no such button")

    def _press(self, mid, verdict, user_id=1):
        data = self._callback_data(self.chat.keyboards[mid], verdict)
        return self.mirror.handle_callback(
            {"id": "cb1", "data": data, "from": {"id": user_id},
             "message": {"message_id": mid, "text": self.chat.messages[mid]}},
            [1])

    # ---- the happy paths
    def test_allow_button_returns_an_allow_decision_to_claude(self):
        thread, box = self._request()
        mid = self._await_card()
        self._press(mid, "a")
        thread.join(5)
        self.assertEqual(box["out"], {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest", "decision": "allow"}})

    def test_deny_button_returns_a_deny_decision_with_a_reason(self):
        thread, box = self._request()
        mid = self._await_card()
        self._press(mid, "d")
        thread.join(5)
        out = box["out"]["hookSpecificOutput"]
        self.assertEqual(out["decision"], "deny")
        self.assertEqual(out["hookEventName"], "PermissionRequest")
        self.assertTrue(out.get("reason"))

    def test_card_shows_the_tool_and_safe_detail(self):
        thread, box = self._request(tool_name="Bash",
                                    tool_input={"command": "rm -rf ./build"})
        mid = self._await_card()
        card = self.chat.messages[mid]
        self.assertIn("Permission needed", card)
        self.assertIn("Bash", card)
        self.assertIn("rm -rf ./build", card)
        self._press(mid, "d")
        thread.join(5)

    def test_answering_clears_the_buttons_and_states_the_outcome(self):
        thread, box = self._request()
        mid = self._await_card()
        self._press(mid, "a")
        thread.join(5)
        self.assertEqual(self.chat.keyboards[mid], mirror_mod.NO_KEYBOARD)
        self.assertIn("Allowed", self.chat.messages[mid])
        self.assertTrue(self.chat.answered)                 # the button spinner was stopped

    # ---- the safe fallbacks
    def test_no_answer_falls_back_to_the_terminal_prompt(self):
        core.bind_session(SID, bridge=BRIDGE, permission_timeout=0.3)
        out = core.handle({"hook_event_name": "PermissionRequest", "session_id": SID,
                           "tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertIsNone(out)                              # no decision → normal flow
        self.assertIn("permission_expired", self.kinds())

    def test_expiry_retracts_the_buttons(self):
        core.bind_session(SID, bridge=BRIDGE, permission_timeout=0.3)
        core.handle({"hook_event_name": "PermissionRequest", "session_id": SID,
                     "tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.mirror.tick()
        mid = next(iter(self.chat.keyboards))
        self.assertEqual(self.chat.keyboards[mid], mirror_mod.NO_KEYBOARD)
        self.assertIn("Expired", self.chat.messages[mid])

    def test_no_bridge_means_no_waiting_at_all(self):
        os.unlink(core.heartbeat_path(BRIDGE))              # bridge is down
        started = time.monotonic()
        out = core.handle({"hook_event_name": "PermissionRequest", "session_id": SID,
                           "tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertIsNone(out)
        self.assertLess(time.monotonic() - started, 1.0)    # must NOT block the session
        self.assertEqual(self.kinds(), [])

    def test_telegram_originated_turns_never_ask_remotely(self):
        self.hook("UserPromptSubmit", user_input="[TG] do it")
        out = core.handle({"hook_event_name": "PermissionRequest", "session_id": SID,
                           "tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertIsNone(out)
        self.assertEqual(self.kinds(), [])

    # ---- authorization
    def test_a_stranger_cannot_decide(self):
        thread, box = self._request()
        mid = self._await_card()
        self._press(mid, "a", user_id=999)                  # not in the allow-list
        self.assertIsNone(core._read_json(core.decision_path(BRIDGE, "x")))
        self.assertEqual(self.chat.keyboards[mid]["inline_keyboard"], [[
            {"text": "✅ Allow", "callback_data": self._callback_data(
                self.chat.keyboards[mid], "a")},
            {"text": "⛔ Deny", "callback_data": self._callback_data(
                self.chat.keyboards[mid], "d")}]])          # buttons still live
        self._press(mid, "d")                               # the owner still can
        thread.join(5)
        self.assertEqual(box["out"]["hookSpecificOutput"]["decision"], "deny")

    def test_a_second_press_is_ignored(self):
        thread, box = self._request()
        mid = self._await_card()
        stale = self._callback_data(self.chat.keyboards[mid], "d")   # captured while live
        self._press(mid, "a")
        thread.join(5)
        self.chat.answered.clear()
        # Telegram removes the buttons, but a client with the message still cached could
        # replay the press. It must decide nothing.
        self.mirror.handle_callback(
            {"id": "cb2", "data": stale, "from": {"id": 1},
             "message": {"message_id": mid, "text": self.chat.messages[mid]}}, [1])
        self.assertEqual(self.chat.answered[-1][1], "That request is no longer waiting.")
        self.assertEqual(box["out"]["hookSpecificOutput"]["decision"], "allow")

    def test_unrelated_callbacks_are_left_alone(self):
        self.assertFalse(self.mirror.handle_callback({"id": "x", "data": "other:thing"}, [1]))

    # ---- housekeeping
    def test_turn_end_retracts_a_card_answered_at_the_keyboard(self):
        core.bind_session(SID, bridge=BRIDGE, permission_timeout=0.3)
        core.handle({"hook_event_name": "PermissionRequest", "session_id": SID,
                     "tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.mirror.tick()
        mid = next(iter(self.chat.keyboards))
        self.mirror._pending_perms[  # re-open it as if it were still awaiting an answer
            core.slug("x")] = {"mid": mid, "ts": 0.0, "session": SID}
        self.hook("Stop", last_assistant_message="done")
        self.mirror.tick()
        self.assertEqual(self.mirror._pending_perms, {})
        self.assertEqual(self.chat.keyboards[mid], mirror_mod.NO_KEYBOARD)

    def test_uncollected_decisions_are_swept(self):
        core.write_decision(BRIDGE, "abc", "allow", by=1)
        path = core.decision_path(BRIDGE, "abc")
        self.assertTrue(os.path.exists(path))
        os.utime(path, (0, 0))                              # pretend it is ancient
        core.sweep_decisions(BRIDGE)
        self.assertFalse(os.path.exists(path))

    def test_permission_detail_never_dumps_raw_mcp_arguments(self):
        detail = core.permission_detail("mcp__vault__read", {"path": "/x", "token": "s3cret"})
        self.assertIn("path", detail)
        self.assertNotIn("s3cret", detail)

    def test_permission_detail_redacts_secrets(self):
        detail = core.permission_detail("Bash", {"command": "export API_TOKEN=hunter2hunter2"})
        self.assertNotIn("hunter2hunter2", detail)


# --------------------------------------------------------------------------- markdown

class MarkdownTests(_StateCase):
    def setUp(self) -> None:
        super().setUp()
        self.chat = _FakeChat()
        self.mirror = _mirror_for(self.chat)
        self._interval = mirror_mod.EDIT_INTERVAL
        mirror_mod.EDIT_INTERVAL = 0.0
        self.addCleanup(setattr, mirror_mod, "EDIT_INTERVAL", self._interval)
        self.enable()

    def display(self, mid, delta, final=False):
        self.hook("MessageDisplay", message_id=mid, turn_id="t1", index=0,
                  delta=delta, final=final)

    def test_bold_and_code_are_rendered(self):
        self.display("m1", "Use **bold** and `code` here.", final=True)
        self.mirror.tick()
        sent = self.chat.stream()[0]
        self.assertIn("<b>bold</b>", sent)
        self.assertIn("<code>code</code>", sent)
        self.assertEqual(self.chat.parse_modes[0], "HTML")

    def test_half_streamed_markdown_stays_literal_not_unbalanced(self):
        self.display("m1", "starting **bold that is not closed yet")
        self.mirror.tick()
        sent = self.chat.stream()[0]
        self.assertNotIn("<b>", sent)               # an unclosed span must not open a tag
        self.assertIn("**bold that is not closed yet", sent)

    def test_rejected_html_falls_back_to_plain_text(self):
        self.chat.reject_html = True
        self.display("m1", "**bold** text", final=True)
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), ["**bold** text"])   # content, never lost

    def test_rejected_html_on_an_edit_still_delivers_the_tail(self):
        self.display("m1", "first part ")
        self.mirror.tick()
        self.chat.reject_html = True
        self.display("m1", "and the tail.", final=True)
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), ["first part and the tail."])

    def test_a_failed_edit_is_retried_rather_than_marked_shown(self):
        self.display("m1", "first ")
        self.mirror.tick()
        self.chat.reject_html = True
        original_edit = self.chat.edit_plain
        self.chat.edit_plain = lambda *a, **k: False        # every edit fails for now
        self.display("m1", "second ")
        self.mirror.tick()
        self.chat.edit_plain = original_edit
        self.chat.reject_html = False
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), rendered("first second "))

    def test_chunking_uses_the_raw_length_so_rendered_text_still_fits(self):
        from agent2telegram.telegram import MAX_MESSAGE_LEN
        self.display("m1", " ".join("a&b" for _ in range(2000)), final=True)
        self.mirror.tick()
        for chunk in self.chat.stream():
            self.assertLessEqual(len(chunk), MAX_MESSAGE_LEN)   # &amp; expansion included


# --------------------------------------------------------------------------- spool

class SpoolTests(_StateCase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()

    def _mirror(self, chat: _FakeChat) -> RemoteControlMirror:
        return _mirror_for(chat)

    def test_events_are_ordered(self):
        for i in range(30):
            core.write_event(BRIDGE, {"type": "prompt", "text": str(i)})
        self.assertEqual([e["text"] for _, e in core.read_events(BRIDGE)],
                         [str(i) for i in range(30)])

    def test_queued_events_survive_a_bridge_restart(self):
        self.hook("UserPromptSubmit", user_input="before the restart")
        chat = _FakeChat()
        self._mirror(chat).tick()                       # a *new* mirror object, as after restart
        self.assertEqual(chat.sent_text, ["🖥️ You:\nbefore the restart"])

    def test_consumed_events_are_not_resent(self):
        self.hook("UserPromptSubmit", user_input="once")
        chat = _FakeChat()
        m = self._mirror(chat)
        self.assertEqual(m.tick(), 1)
        self.assertEqual(m.tick(), 0)
        self.assertEqual(len(chat.sent_text), 1)
        self.assertEqual(core.pending_count(BRIDGE), 0)

    def test_malformed_event_does_not_wedge_the_queue(self):
        core._write_private(os.path.join(core.events_dir(BRIDGE), "00000000-bad.json"),
                            "{not json")
        self.hook("UserPromptSubmit", user_input="after the bad one")
        chat = _FakeChat()
        self._mirror(chat).tick()
        self.assertEqual(chat.sent_text, ["🖥️ You:\nafter the bad one"])
        self.assertEqual(core.pending_count(BRIDGE), 0)

    def test_spool_is_capped_when_the_consumer_is_gone(self):
        for i in range(core.MAX_PENDING + 20):
            core.write_event(BRIDGE, {"type": "prompt", "text": str(i)})
        self.assertLessEqual(core.pending_count(BRIDGE), core.MAX_PENDING)

    def test_heartbeat_lets_the_spool_grow_while_the_bridge_is_live(self):
        core.touch_heartbeat(BRIDGE)
        for i in range(core.MAX_PENDING + 20):
            core.write_event(BRIDGE, {"type": "prompt", "text": str(i)})
        self.assertGreater(core.pending_count(BRIDGE), core.MAX_PENDING)

    def test_mirror_prunes_a_runaway_spool(self):
        core.touch_heartbeat(BRIDGE)
        for i in range(mirror_mod.DROP_ABOVE + 50):
            core.write_event(BRIDGE, {"type": "notification", "notification_type": "x"})
        chat = _FakeChat()
        self._mirror(chat).tick()
        self.assertLessEqual(core.pending_count(BRIDGE), mirror_mod.DROP_ABOVE)


# --------------------------------------------------------------------------- blocking dialogs

class BlockingDialogTests(_StateCase):
    """Surface-only mode: a session that has STOPPED to ask must not look like a busy one.

    Remote answering is covered separately in :class:`QuestionAnswerTests`; here it is switched
    off, which is also what happens whenever no bridge is consuming.
    """

    def setUp(self) -> None:
        super().setUp()
        self.chat = _FakeChat()
        self.mirror = _mirror_for(self.chat)
        core.bind_session(SID, bridge=BRIDGE, permissions=False)

    QUESTION = {"questions": [{"header": "Approach", "question": "Which way?",
                               "options": [{"label": "Rewrite", "description": "start over"},
                                           {"label": "Patch", "description": "minimal"}],
                               "multiSelect": False}]}

    def test_ask_user_question_is_reported_not_treated_as_a_tool(self):
        self.hook("PreToolUse", tool_name="AskUserQuestion", tool_input=self.QUESTION,
                  tool_use_id="tu1")
        self.assertEqual(self.kinds(), ["question"])
        self.mirror.tick()
        card = self.chat.messages[self.chat.order[0]]
        self.assertIn("Waiting for your answer", card)
        self.assertIn("Which way?", card)
        self.assertIn("1. Rewrite", card)
        self.assertIn("2. Patch", card)
        self.assertEqual(self.chat.status, [])      # not a transient tool bubble

    def test_typing_stops_while_the_session_is_blocked(self):
        self.display_working()
        self.assertEqual(self.chat.active[-1], True)
        self.hook("PreToolUse", tool_name="AskUserQuestion", tool_input=self.QUESTION,
                  tool_use_id="tu1")
        self.mirror.tick()
        self.assertEqual(self.chat.active[-1], False)   # waiting for a human, not working

    def test_typing_resumes_once_the_question_is_answered(self):
        self.display_working()
        self.hook("PreToolUse", tool_name="AskUserQuestion", tool_input=self.QUESTION,
                  tool_use_id="tu1")
        self.mirror.tick()
        self.hook("PostToolUse", tool_name="AskUserQuestion", tool_use_id="tu1")
        self.mirror.tick()
        self.assertEqual(self.chat.active[-1], True)
        self.assertIn("Answered at the terminal", self.chat.messages[self.chat.order[-1]])

    def test_post_tool_use_for_ordinary_tools_is_ignored(self):
        # Registered with a matcher, but never trust the matcher for something this noisy.
        self.hook("PostToolUse", tool_name="Bash", tool_use_id="tu9",
                  tool_output="secret output")
        self.assertEqual(self.kinds(), [])

    def test_question_text_is_redacted_and_capped(self):
        self.hook("PreToolUse", tool_name="AskUserQuestion", tool_use_id="tu1",
                  tool_input={"questions": [{"header": "h", "question": "use API_TOKEN=abc123xyz789?",
                                             "options": [{"label": "y" * 300}]}]})
        ev = self.events()[0]
        self.assertNotIn("abc123xyz789", json.dumps(ev))
        self.assertLessEqual(len(ev["questions"][0]["options"][0]), 61)

    def test_mcp_elicitation_is_surfaced_and_cleared(self):
        self.hook("Elicitation", server_name="vault", elicitation_id="e1",
                  prompt="Which secret do you want?")
        self.mirror.tick()
        self.assertIn("vault", self.chat.messages[self.chat.order[0]])
        self.hook("ElicitationResult", elicitation_id="e1", user_response="x")
        self.mirror.tick()
        self.assertIn("Answered at the terminal", self.chat.messages[self.chat.order[0]])

    def test_turn_end_closes_an_unanswered_dialog(self):
        self.hook("PreToolUse", tool_name="AskUserQuestion", tool_input=self.QUESTION,
                  tool_use_id="tu1")
        self.hook("Stop", last_assistant_message="")
        self.mirror.tick()
        self.assertEqual(self.mirror._session(SID).waiting, {})

    def display_working(self):
        self.hook("MessageDisplay", message_id="m1", turn_id="t1", index=0,
                  delta="thinking…", final=False)
        self.mirror.tick()


# --------------------------------------------------------------------------- answering

class QuestionAnswerTests(_StateCase):
    """Answering Claude's own question from the chat.

    There is no hook output that supplies a tool RESULT, so the answer rides back the documented
    way: block AskUserQuestion with `deny` and put the choice in permissionDecisionReason, which
    Claude Code shows to the model.
    """

    SINGLE = {"questions": [{"header": "Approach", "question": "Rewrite or patch?",
                             "options": [{"label": "Rewrite"}, {"label": "Patch"}],
                             "multiSelect": False}]}
    MULTI = {"questions": [{"header": "Targets", "question": "Which files?",
                            "options": [{"label": "importer.ts"}, {"label": "reconcile.ts"},
                                        {"label": "types.ts"}],
                            "multiSelect": True}]}
    TWO = {"questions": [
        {"header": "Approach", "question": "Rewrite or patch?",
         "options": [{"label": "Rewrite"}, {"label": "Patch"}], "multiSelect": False},
        {"header": "Tests", "question": "Run them?",
         "options": [{"label": "Yes"}, {"label": "No"}], "multiSelect": False}]}

    def setUp(self) -> None:
        super().setUp()
        self.chat = _FakeChat()
        self.mirror = _mirror_for(self.chat)
        self.enable()
        core.touch_heartbeat(BRIDGE)          # a bridge is listening → answering is possible

    def _ask(self, tool_input, timeout=6.0):
        core.bind_session(SID, bridge=BRIDGE, question_timeout=timeout)
        core.touch_heartbeat(BRIDGE)
        box = {}

        def run():
            box["out"] = core.handle({"hook_event_name": "PreToolUse", "session_id": SID,
                                      "tool_name": "AskUserQuestion", "tool_use_id": "tu1",
                                      "tool_input": tool_input})

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, box

    def _await_card(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.mirror.tick()
            if self.chat.keyboards:
                return next(iter(self.chat.keyboards))
            time.sleep(0.02)
        self.fail("no question card was posted")

    def _press(self, mid, label, user_id=1):
        for row in self.chat.keyboards[mid]["inline_keyboard"]:
            for button in row:
                if button["text"].lstrip("✅ ").startswith(label):
                    return self.mirror.handle_callback(
                        {"id": "cb", "data": button["callback_data"], "from": {"id": user_id},
                         "message": {"message_id": mid, "text": self.chat.messages[mid]}}, [1])
        raise AssertionError(f"no button matching {label!r}")

    @staticmethod
    def _reason(box):
        return box["out"]["hookSpecificOutput"]["permissionDecisionReason"]

    # ---- the happy paths
    def test_one_tap_answers_a_single_choice_question(self):
        thread, box = self._ask(self.SINGLE)
        mid = self._await_card()
        self._press(mid, "Rewrite")
        thread.join(5)
        out = box["out"]["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("Approach: Rewrite", out["permissionDecisionReason"])

    def test_the_model_is_told_not_to_ask_again(self):
        thread, box = self._ask(self.SINGLE)
        self._press(self._await_card(), "Patch")
        thread.join(5)
        self.assertIn("do not ask again", self._reason(box))

    def test_multi_select_toggles_and_waits_for_send(self):
        thread, box = self._ask(self.MULTI)
        mid = self._await_card()
        self._press(mid, "importer.ts")
        self.assertTrue(thread.is_alive())          # nothing submitted yet
        self._press(mid, "types.ts")
        self._press(mid, "📨")
        thread.join(5)
        reason = self._reason(box)
        self.assertIn("importer.ts", reason)
        self.assertIn("types.ts", reason)
        self.assertNotIn("reconcile.ts", reason)

    def test_multi_select_can_be_deselected(self):
        thread, box = self._ask(self.MULTI)
        mid = self._await_card()
        self._press(mid, "importer.ts")
        self._press(mid, "importer.ts")             # tap again to unselect
        self._press(mid, "reconcile.ts")
        self._press(mid, "📨")
        thread.join(5)
        self.assertNotIn("importer.ts", self._reason(box))
        self.assertIn("reconcile.ts", self._reason(box))

    def test_several_questions_are_all_collected(self):
        thread, box = self._ask(self.TWO)
        mid = self._await_card()
        self._press(mid, "Patch")
        self.assertTrue(thread.is_alive())          # more than one question → explicit send
        self._press(mid, "Yes")
        self._press(mid, "📨")
        thread.join(5)
        reason = self._reason(box)
        self.assertIn("Approach: Patch", reason)
        self.assertIn("Tests: Yes", reason)

    def test_send_with_nothing_chosen_does_not_submit(self):
        thread, box = self._ask(self.MULTI)
        mid = self._await_card()
        self._press(mid, "📨")
        self.assertTrue(thread.is_alive())
        self.assertEqual(self.chat.answered[-1][1], "Choose an option first.")
        self._press(mid, "types.ts")
        self._press(mid, "📨")
        thread.join(5)
        self.assertIn("types.ts", self._reason(box))

    def test_a_free_text_reply_is_the_answer(self):
        thread, box = self._ask(self.SINGLE)
        mid = self._await_card()
        self.assertTrue(self.mirror.answer_question_reply(mid, "neither — do it in two passes"))
        thread.join(5)
        self.assertIn("two passes", self._reason(box))

    def test_a_reply_to_something_else_is_not_swallowed(self):
        self._ask(self.SINGLE)
        self._await_card()
        self.assertFalse(self.mirror.answer_question_reply(999999, "unrelated message"))

    # ---- the card
    def test_the_card_shows_the_options_and_the_selection(self):
        thread, box = self._ask(self.MULTI)
        mid = self._await_card()
        self.assertIn("Which files?", self.chat.messages[mid])
        self.assertIn("importer.ts", self.chat.messages[mid])
        self._press(mid, "importer.ts")
        self.assertIn("✅ importer.ts", self.chat.messages[mid])
        self._press(mid, "📨")
        thread.join(5)
        self.assertIn("Answered", self.chat.messages[mid])
        self.assertEqual(self.chat.keyboards[mid], mirror_mod.NO_KEYBOARD)

    def test_typing_stops_while_the_question_is_open(self):
        self.hook("MessageDisplay", message_id="m1", turn_id="t", index=0, delta="working")
        self.mirror.tick()
        self.assertEqual(self.chat.active[-1], True)
        thread, box = self._ask(self.SINGLE)
        mid = self._await_card()
        self.assertEqual(self.chat.active[-1], False)
        self._press(mid, "Rewrite")
        thread.join(5)
        self.assertEqual(self.chat.active[-1], True)

    # ---- the safe fallbacks
    def test_no_answer_falls_back_to_the_terminal_picker(self):
        core.bind_session(SID, bridge=BRIDGE, question_timeout=0.3)
        core.touch_heartbeat(BRIDGE)
        out = core.handle({"hook_event_name": "PreToolUse", "session_id": SID,
                           "tool_name": "AskUserQuestion", "tool_use_id": "tu1",
                           "tool_input": self.SINGLE})
        self.assertIsNone(out)                        # no decision → the tool runs as normal
        self.assertIn("question_expired", self.kinds())

    def test_no_bridge_means_report_only_and_no_waiting(self):
        os.unlink(core.heartbeat_path(BRIDGE))
        started = time.monotonic()
        out = core.handle({"hook_event_name": "PreToolUse", "session_id": SID,
                           "tool_name": "AskUserQuestion", "tool_use_id": "tu1",
                           "tool_input": self.SINGLE})
        self.assertIsNone(out)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.kinds(), ["question"])   # reported, not asked

    def test_remote_decisions_off_reports_only(self):
        core.bind_session(SID, bridge=BRIDGE, permissions=False)
        out = core.handle({"hook_event_name": "PreToolUse", "session_id": SID,
                           "tool_name": "AskUserQuestion", "tool_use_id": "tu1",
                           "tool_input": self.SINGLE})
        self.assertIsNone(out)
        self.assertEqual(self.kinds(), ["question"])

    def test_ordinary_tools_never_block(self):
        started = time.monotonic()
        core.handle({"hook_event_name": "PreToolUse", "session_id": SID,
                     "tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(self.kinds(), ["tool"])

    def test_a_stranger_cannot_answer(self):
        thread, box = self._ask(self.SINGLE)
        mid = self._await_card()
        self._press(mid, "Rewrite", user_id=999)
        self.assertTrue(thread.is_alive())            # still waiting
        self.assertEqual(self.chat.answered[-1][1], "Not authorized.")
        self._press(mid, "Rewrite")
        thread.join(5)
        self.assertIn("Rewrite", self._reason(box))

    def test_turn_end_retracts_an_unanswered_question(self):
        thread, box = self._ask(self.SINGLE, timeout=4.0)
        mid = self._await_card()
        self.hook("Stop", last_assistant_message="")
        self.mirror.tick()
        self.assertEqual(self.chat.keyboards[mid], mirror_mod.NO_KEYBOARD)
        thread.join(6)


# --------------------------------------------------------------------------- compaction

class CompactionTests(_StateCase):
    def setUp(self) -> None:
        super().setUp()
        self.chat = _FakeChat()
        self.mirror = _mirror_for(self.chat)
        self.enable()

    def test_compaction_is_explained_rather_than_a_silent_gap(self):
        self.hook("PreCompact", trigger="auto")
        self.mirror.tick()
        self.assertIn("Compacting", self.chat.status[-1])
        self.hook("PostCompact", trigger="auto")
        self.mirror.tick()
        self.assertIn("compacted", self.chat.sent_text[-1])
        self.assertIn("auto", self.chat.sent_text[-1])

    def test_compaction_does_not_disconnect(self):
        self.hook("PreCompact", trigger="manual")
        self.hook("PostCompact", trigger="manual")
        self.hook("SessionStart", source="compact")
        self.assertTrue(core.is_enabled(SID))


# --------------------------------------------------------------------------- recap

class RecapTests(_StateCase):
    def _transcript(self, session_id, cwd, records):
        d = Path(self._tmp.name) / "claude" / "projects" / cwd.replace("/", "-")
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(self._tmp.name) / "claude")
        self.addCleanup(os.environ.pop, "CLAUDE_CONFIG_DIR", None)

    @staticmethod
    def _msg(kind, text):
        return {"type": kind, "message": {"content": [{"type": "text", "text": text}]}}

    def test_recap_summarizes_the_recent_conversation(self):
        self._transcript(SID, "/work/proj", [
            {"type": "summary"},
            self._msg("user", "first question"),
            self._msg("assistant", "first answer"),
            {"type": "user", "message": {"content": [{"type": "tool_result", "text": "ignored"}]}},
            self._msg("user", "second question"),
            self._msg("assistant", "second answer"),
        ])
        text = core.recap(SID, "/work/proj")
        self.assertIn("second question", text)
        self.assertIn("second answer", text)
        self.assertNotIn("ignored", text)          # tool results are not conversation

    def test_recap_keeps_only_the_last_few_and_truncates_them(self):
        self._transcript(SID, "/work/proj",
                         [self._msg("assistant", f"msg{i} " + "x" * 2000) for i in range(20)])
        text = core.recap(SID, "/work/proj", limit=2)
        self.assertEqual(text.count("🤖:"), 2)
        self.assertLess(len(text), 2 * (core.RECAP_CHARS + 40))

    def test_recap_survives_a_tail_full_of_tool_records(self):
        # A real transcript's tail is mostly tool_use/tool_result with no text; the window has
        # to be big enough to reach actual conversation or the digest is one lonely line.
        records = []
        for i in range(6):
            records.append(self._msg("user", f"question {i}"))
            records.append(self._msg("assistant", f"answer {i}"))
            records.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "text": "T" * 30000}]}})
        self._transcript(SID, "/work/proj", records)
        text = core.recap(SID, "/work/proj")
        self.assertEqual(text.count("🖥️ You:") + text.count("🤖:"), core.RECAP_MESSAGES)
        self.assertIn("answer 5", text)

    def test_recap_is_empty_when_there_is_no_transcript(self):
        self.assertEqual(core.recap(SID, "/nowhere"), "")
        self.assertEqual(core.recap("", "/work/proj"), "")

    def test_recap_reaches_telegram_as_a_digest(self):
        self.enable()
        core.write_event(BRIDGE, {"type": "recap", "session_id": SID,
                                  "text": "🖥️ You: hi\n\n🤖: hello", "cwd": "/work/proj"})
        chat = _FakeChat()
        _mirror_for(chat).tick()
        self.assertIn("Where this session is up to", chat.sent_text[0])
        self.assertIn("hello", chat.sent_text[0])


# --------------------------------------------------------------------------- multi-session

class MultiSessionTests(_StateCase):
    """Two sessions on one bridge must not share state or interleave unlabelled."""

    def setUp(self) -> None:
        super().setUp()
        self.chat = _FakeChat()
        self.mirror = _mirror_for(self.chat)
        self._interval = mirror_mod.EDIT_INTERVAL
        mirror_mod.EDIT_INTERVAL = 0.0
        self.addCleanup(setattr, mirror_mod, "EDIT_INTERVAL", self._interval)
        core.bind_session(SID, bridge=BRIDGE, cwd="/work/alpha")
        core.bind_session(OTHER, bridge=BRIDGE, cwd="/work/beta")

    def _display(self, sid, mid, delta, final=False):
        core.handle({"hook_event_name": "MessageDisplay", "session_id": sid,
                     "message_id": mid, "turn_id": "t", "index": 0,
                     "delta": delta, "final": final})

    def test_each_session_gets_its_own_message(self):
        self._display(SID, "m1", "alpha speaking")
        self._display(OTHER, "m1", "beta speaking")     # same message_id, different session
        self.mirror.tick()
        self.assertEqual(len(self.chat.order), 2)

    def test_messages_are_labelled_when_two_sessions_are_mirrored(self):
        self._display(SID, "m1", "alpha speaking", final=True)
        self.mirror.tick()
        self.assertIn("[alpha]", self.chat.stream()[0])

    def test_one_session_is_not_labelled(self):
        core.unbind_session(OTHER)
        self._display(SID, "m1", "alone", final=True)
        self.mirror.tick()
        self.assertNotIn("[", self.chat.stream()[0])

    def test_one_sessions_turn_end_does_not_finalize_the_other(self):
        self._display(SID, "m1", "alpha still typing")
        self._display(OTHER, "m2", "beta still typing")
        self.mirror.tick()
        core.handle({"hook_event_name": "Stop", "session_id": SID,
                     "last_assistant_message": "alpha done"})
        self.mirror.tick()
        self.assertEqual(self.mirror._session(SID).live, {})
        self.assertNotEqual(self.mirror._session(OTHER).live, {})   # untouched

    def test_typing_stays_on_while_the_other_session_works(self):
        self._display(SID, "m1", "alpha")
        self._display(OTHER, "m2", "beta")
        self.mirror.tick()
        self.assertEqual(self.chat.active[-1], True)
        core.handle({"hook_event_name": "Stop", "session_id": SID, "last_assistant_message": ""})
        self.mirror.tick()
        self.assertEqual(self.chat.active[-1], True)     # beta is still going
        core.handle({"hook_event_name": "Stop", "session_id": OTHER, "last_assistant_message": ""})
        self.mirror.tick()
        self.assertEqual(self.chat.active[-1], False)

    def test_a_permission_card_is_retracted_only_for_its_own_session(self):
        core.touch_heartbeat(BRIDGE)
        core.write_event(BRIDGE, {"type": "permission_request", "session_id": OTHER,
                                  "request_id": "r1", "tool_name": "Bash", "summary": "x"})
        self.mirror.tick()
        mid = next(iter(self.chat.keyboards))
        core.handle({"hook_event_name": "Stop", "session_id": SID, "last_assistant_message": ""})
        self.mirror.tick()
        self.assertNotEqual(self.chat.keyboards[mid], mirror_mod.NO_KEYBOARD)  # still live
        core.handle({"hook_event_name": "Stop", "session_id": OTHER, "last_assistant_message": ""})
        self.mirror.tick()
        self.assertEqual(self.chat.keyboards[mid], mirror_mod.NO_KEYBOARD)


# --------------------------------------------------------------------------- interrupt

class InterruptTests(_StateCase):
    def setUp(self) -> None:
        super().setUp()
        self.chat = _FakeChat()
        self.mirror = _mirror_for(self.chat)
        self.enable()

    def test_interrupt_ends_the_turn_that_stop_will_never_end(self):
        self.hook("MessageDisplay", message_id="m1", turn_id="t1", index=0,
                  delta="half an answer", final=False)
        self.mirror.tick()
        self.assertEqual(self.chat.active[-1], True)
        core.write_event(BRIDGE, {"type": "interrupted", "session_id": ""})
        self.mirror.tick()
        self.assertEqual(self.chat.active[-1], False)
        self.assertEqual(self.chat.stream(), rendered("half an answer"))  # partial text kept
        self.assertEqual(self.mirror._session(SID).live, {})

    def test_passthrough_command_set_matches_what_the_agent_understands(self):
        from agent2telegram.attach import PASSTHROUGH_COMMANDS
        for cmd in ("compact", "clear", "context", "model", "effort", "mcp", "config"):
            self.assertIn(cmd, PASSTHROUGH_COMMANDS)
        # /exit would end the session in the tmux seat and take the remote side down with it.
        self.assertNotIn("exit", PASSTHROUGH_COMMANDS)

    def test_raw_injection_does_not_prepend_the_origin_prefix(self):
        from agent2telegram.session import TmuxSession
        sent = []

        class _Fake(TmuxSession):
            def __init__(self):                      # bypass the tmux existence check
                self._origin = "[TG] "
                self.name = "x"

            def _send_keys(self, text):
                sent.append((self._origin, text))

            @property
            def alive(self):
                return True

        fake = _Fake()
        fake.inject_raw("/compact")
        self.assertEqual(sent, [("", "/compact")])
        self.assertEqual(fake._origin, "[TG] ")      # restored afterwards


# --------------------------------------------------------------------------- bridge start

class SuperviseTests(_StateCase):
    """The one rule: never start a second poller for the same bot token."""

    def setUp(self) -> None:
        super().setUp()
        from agent2telegram.remote_control import supervise
        self.supervise = supervise

    def test_a_fresh_heartbeat_plus_a_process_means_running(self):
        core.touch_heartbeat(BRIDGE)
        self._patch("running_process", lambda _cfg: "python -m agent2telegram run")
        self.assertEqual(self.supervise.status(BRIDGE, "/x/config.json")[0], "consuming")

    def test_a_fresh_heartbeat_with_no_process_is_a_dead_bridge(self):
        # The bug this guards: a bridge killed a second ago still has a one-second-old
        # heartbeat, and the toggle happily reported "consuming" while nothing was running.
        core.touch_heartbeat(BRIDGE)
        self._patch("running_process", lambda _cfg: "")
        self.assertEqual(self.supervise.status(BRIDGE, "/x/config.json")[0], "stopped")

    def test_an_uninspectable_process_list_falls_back_to_the_heartbeat(self):
        core.touch_heartbeat(BRIDGE)
        self._patch("running_process", lambda _cfg: None)
        self.assertEqual(self.supervise.status(BRIDGE, "/x/config.json")[0], "consuming")

    def test_an_uninspectable_process_list_never_claims_stopped_on_a_live_beat(self):
        self._patch("running_process", lambda _cfg: None)
        self.assertEqual(self.supervise.status(BRIDGE, "/x/config.json")[0], "stopped")

    def _patch(self, name, value):
        original = getattr(self.supervise, name)
        setattr(self.supervise, name, value)
        self.addCleanup(setattr, self.supervise, name, original)

    def test_nothing_running_reports_stopped(self):
        self._patch("running_process", lambda _cfg: "")
        self.assertEqual(self.supervise.status(BRIDGE, "/x/config.json")[0], "stopped")

    def test_ensure_running_is_a_no_op_when_already_consuming(self):
        core.touch_heartbeat(BRIDGE)
        self._patch("running_process", lambda _cfg: "python -m agent2telegram run")
        ok, message = self.supervise.ensure_running(BRIDGE, "/x/config.json")
        self.assertTrue(ok)
        self.assertIn("already running", message)

    def test_a_process_that_is_not_consuming_is_never_duplicated(self):
        self._patch("running_process", lambda _cfg: "python -m agent2telegram run")
        self._patch("RECHECK", 0.2)               # do not really wait out a flood-control sleep
        launched = []
        self._patch("_launch", lambda *a, **k: launched.append(a))
        ok, message = self.supervise.ensure_running(BRIDGE, "/x/config.json", timeout=0.1)
        self.assertFalse(ok)
        self.assertIn("not draining", message)
        self.assertEqual(launched, [])            # crucially: we did NOT start another one

    def test_the_start_lock_admits_only_one_starter(self):
        self.assertTrue(self.supervise._acquire_lock(BRIDGE))
        self.assertFalse(self.supervise._acquire_lock(BRIDGE))
        self.supervise._release_lock(BRIDGE)
        self.assertTrue(self.supervise._acquire_lock(BRIDGE))

    def test_a_crashed_starter_does_not_wedge_the_lock_forever(self):
        self.assertTrue(self.supervise._acquire_lock(BRIDGE))
        lock = os.path.join(core.bridge_dir(BRIDGE), "start.lock")
        os.utime(lock, (0, 0))                    # left behind long ago
        self.assertTrue(self.supervise._acquire_lock(BRIDGE))

    def test_session_name_is_derived_from_the_bridge(self):
        self.assertEqual(self.supervise.session_name("qwen telegram"), "a2t-qwen_telegram")


# --------------------------------------------------------------------------- installer

class InstallerTests(_StateCase):
    """The hook merge must be additive and idempotent — it edits a file users depend on."""

    def setUp(self) -> None:
        super().setUp()
        import contextlib
        import io
        from agent2telegram.remote_control import install as install_mod
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.install_mod = install_mod
        self.cdir = Path(self._tmp.name) / "claude"
        (self.cdir / "skills").mkdir(parents=True)
        self.settings = self.cdir / "settings.json"
        self.foreign = {"type": "command", "command": "/opt/peon-ping/peon.sh", "timeout": 10}
        self.settings.write_text(json.dumps({
            "hooks": {
                "Stop": [{"matcher": "", "hooks": [self.foreign]},
                         {"hooks": [{"type": "command",
                                     "command": "/nonexistent/bin/qwen-telegram-stop-hook"}]}],
                "SessionStart": [{"matcher": "", "hooks": [self.foreign]}],
            }
        }), encoding="utf-8")
        cfg = Path(self._tmp.name) / "bridge.json"
        cfg.write_text(json.dumps({"agent": "claude-code", "mode": "attach",
                                   "tmux_session": BRIDGE, "origin_prefix": "[TG] "}),
                       encoding="utf-8")
        self.cfg = cfg

    def _args(self, **kw):
        base = dict(claude_config_dir=str(self.cdir), agent2telegram_config=str(self.cfg),
                    tmux_session=BRIDGE, skill_name="local-remote", label="Test Remote",
                    python="python3", dry_run=False, keep_state=False)
        base.update(kw)
        return type("A", (), base)()

    def _hooks(self):
        return json.loads(self.settings.read_text("utf-8"))["hooks"]

    def test_install_registers_every_event_and_keeps_foreign_hooks(self):
        self.assertEqual(self.install_mod.install(self._args()), 0)
        hooks = self._hooks()
        for event in self.install_mod.EVENT_TIMEOUTS:
            blob = json.dumps(hooks[event])
            self.assertIn("remote_control", blob, event)
        self.assertIn(self.foreign, [h for g in hooks["Stop"] for h in g["hooks"]])
        self.assertIn(self.foreign, [h for g in hooks["SessionStart"] for h in g["hooks"]])

    def test_install_removes_the_legacy_wrapper(self):
        self.install_mod.install(self._args())
        self.assertNotIn("qwen-telegram-stop-hook", self.settings.read_text("utf-8"))

    def test_stop_events_also_get_the_agent2telegram_turn_end_signal(self):
        self.install_mod.install(self._args())
        hooks = self._hooks()
        for event in self.install_mod.STOP_EVENTS:
            self.assertIn("agent2telegram.stop_hook", json.dumps(hooks[event]))

    def test_install_is_idempotent(self):
        self.install_mod.install(self._args())
        first = self.settings.read_text("utf-8")
        self.install_mod.install(self._args())
        self.assertEqual(first, self.settings.read_text("utf-8"))

    def test_install_writes_a_runnable_skill_without_placeholders(self):
        self.install_mod.install(self._args())
        skill = self.cdir / "skills" / "local-remote" / "SKILL.md"
        script = self.cdir / "skills" / "local-remote" / "scripts" / "remote.sh"
        self.assertTrue(skill.exists() and script.exists())
        for path in (skill, script):
            self.assertNotIn("{{", path.read_text("utf-8"), path)
        self.assertIn("disable-model-invocation: true", skill.read_text("utf-8"))
        self.assertTrue(os.access(script, os.X_OK))

    def test_install_backs_up_settings(self):
        self.install_mod.install(self._args())
        self.assertTrue(list(self.cdir.glob("settings.json.bak-remote-control-*")))

    def test_uninstall_removes_only_our_entries(self):
        self.install_mod.install(self._args())
        self.install_mod.uninstall(self._args())
        hooks = self._hooks()
        self.assertNotIn("remote_control", json.dumps(hooks))
        self.assertIn(self.foreign, [h for g in hooks["SessionStart"] for h in g["hooks"]])
        self.assertFalse((self.cdir / "skills" / "local-remote").exists())

    def test_dry_run_changes_nothing(self):
        before = self.settings.read_text("utf-8")
        self.install_mod.install(self._args(dry_run=True))
        self.assertEqual(before, self.settings.read_text("utf-8"))
        self.assertFalse((self.cdir / "skills" / "local-remote").exists())


if __name__ == "__main__":
    unittest.main()
