"""Tests for local Remote Control: state, hook classification, spool and the Telegram mirror.

Standard library only (``python -m unittest discover -s tests``), no network, no tmux and no
real Claude Code session — the hook side is pure filesystem work and the mirror side takes its
Telegram operations as callables, so both are directly observable.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent2telegram.remote_control import core
from agent2telegram.remote_control import mirror as mirror_mod
from agent2telegram.remote_control.mirror import RemoteControlMirror

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
        core.handle({"hook_event_name": event, "session_id": fields.pop("session_id", SID),
                     **fields})

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
        core.write_event(BRIDGE, {"type": "prompt", "text": "hi"})
        for path in (core.enabled_marker(BRIDGE, SID), core.session_index(SID),
                     *(p for p, _ in core.read_events(BRIDGE))):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, path)
        self.assertEqual(os.stat(core.events_dir(BRIDGE)).st_mode & 0o777, 0o700)
        core.touch_heartbeat(BRIDGE)
        self.assertEqual(os.stat(core.heartbeat_path(BRIDGE)).st_mode & 0o777, 0o600)


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

    def test_hook_never_raises_on_garbage(self):
        self.assertEqual(core.handle({}), 0)
        self.assertEqual(core.handle({"hook_event_name": "MessageDisplay"}), 0)
        self.assertEqual(core.handle({"hook_event_name": "Nonsense", "session_id": SID}), 0)


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
        self.cleared = 0
        self._next = 100
        self.fail_send = False

    # streaming primitives
    def send_plain_id(self, text: str):
        if self.fail_send:
            return None
        self._next += 1
        self.messages[self._next] = text
        self.order.append(self._next)
        return self._next

    def edit_plain(self, mid: int, text: str) -> None:
        self.messages[mid] = text

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
        self.mirror = RemoteControlMirror(
            BRIDGE, send_plain_id=self.chat.send_plain_id, edit_plain=self.chat.edit_plain,
            send_text=self.chat.send_text, status_push=self.chat.status_push,
            status_clear=self.chat.status_clear, set_active=self.chat.set_active)
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
        self.assertEqual(self.chat.stream(), ["I'll inspect the importer."])

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
        self.assertEqual(self.mirror._live, {})

    def test_empty_final_delta_still_finalizes(self):
        self.display("m1", "Partial")
        self.mirror.tick()
        self.display("m1", "", final=True)
        self.mirror.tick()
        self.assertEqual(self.chat.stream(), ["Partial"])
        self.assertEqual(self.mirror._live, {})

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

    def test_permission_request_notifies_without_approving(self):
        self.hook("PermissionRequest", tool_name="Bash",
                  tool_input={"description": "delete things"})
        self.mirror.tick()
        self.assertEqual(len(self.chat.sent_text), 1)
        self.assertIn("Waiting for permission", self.chat.sent_text[0])
        self.assertIn("terminal", self.chat.sent_text[0])

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
        self.assertEqual(self.mirror._live, {})

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
        self.assertEqual(self.mirror._live, {})


# --------------------------------------------------------------------------- spool

class SpoolTests(_StateCase):
    def setUp(self) -> None:
        super().setUp()
        self.enable()

    def _mirror(self, chat: _FakeChat) -> RemoteControlMirror:
        return RemoteControlMirror(
            BRIDGE, send_plain_id=chat.send_plain_id, edit_plain=chat.edit_plain,
            send_text=chat.send_text, status_push=chat.status_push,
            status_clear=chat.status_clear, set_active=chat.set_active)

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
