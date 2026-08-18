# Changelog

What this fork adds to [Agent2Telegram](https://github.com/petrludwig-collab/Agent2Telegram).
Upstream's own behaviour — the Telegram transport, attach mode, retries, flood control, media,
voice, self-tests — is unchanged and its tests still pass.

## Unreleased (1.2.0+local-remote.1)

### Fixed — every message arriving twice

`UserPromptSubmit` carries the prompt in **`prompt`**. This read `user_input`, a name taken from
a documentation summary rather than the wire. Every prompt therefore read as empty, every turn
classified as `terminal`, and Telegram-originated turns were mirrored *as well as* forwarded by
the attach path — two copies of everything. Local prompts were never mirrored either, for the
same reason. Auto-compaction made it stickier: `SessionStart(compact)` reset the turn origin, and
compaction fires *mid-turn*, so even a correctly classified Telegram turn flipped to `terminal`
halfway through. The origin is now owned solely by `UserPromptSubmit`, an unreadable prompt never
reclassifies a turn, and `StopFailure`'s real fields (`error`, `last_assistant_message`) are used
instead of the invented `error_type`/`error_message`.

The unit tests did not catch any of this because they fabricated payloads with the same wrong
key. Field names are now asserted against payloads captured from a live Claude Code run.


### Quiet by default

Telegram notifies once per new message and never for an edit, so a twelve-message turn meant
twelve buzzes. Progress (prompt echo, streamed text, tool bubble, recap, compaction) is now sent
with `disable_notification` — delivered and kept in the history, just silent — while permission
cards, questions, elicitations, failures, actionable notifications and one end-of-turn line still
notify. The end-of-turn line previews the answer so a lock screen shows something useful, and is
skipped when the turn had no output or when the whole answer was delivered at the end anyway.
`--loud` restores the old behaviour.

### Hook-based local Remote Control

The core of the fork. Native Claude Code Remote Control is disabled whenever `ANTHROPIC_BASE_URL`
points somewhere other than `api.anthropic.com`, which rules out any harness routed through CCR or
another gateway. This rebuilds the remote surface out of documented Claude Code hooks.

* `agent2telegram/remote_control/` — hook adapter, durable event spool, Telegram mirror, CLI,
  installer, bridge supervision, and the runtime toggle Skill template.
* Assistant text streams from `MessageDisplay`: created on the first delta, edited at most every
  0.6 s, finalized on `final`, continued into a new message past Telegram's size limit rather than
  truncated.
* Tool, subagent and task activity in one temporary trailing status bubble.
* `Stop` finalizes the turn and only sends `last_assistant_message` when nothing was streamed.
  `StopFailure` ends the turn on its own, since `Stop` never fires after an API error.
* Per-session opt-in. `/compact` and `/clear` keep the connection; `startup`, `resume` and `fork`
  reset it to off, matching native's rule.
* Telegram-originated turns are classified at `UserPromptSubmit` and never mirrored, so upstream's
  transcript path keeps sole ownership of them and no answer is ever delivered twice.
* The hook adapter is standard-library only with no relative imports, registered as a plain script
  under `python3 -S -E`: about 22 ms per `MessageDisplay`, no network, no subprocess, no transcript
  read, and it fails open so a broken mirror can never break Claude Code.

### Deciding from the chat

* **Permission approval.** `PermissionRequest` posts an Allow/Deny card and blocks on a local
  decision file; the press arrives on the bridge's existing poller and returns the documented
  `hookSpecificOutput.decision`. Unanswered within `--permission-timeout` (90 s), the terminal
  prompt takes over.
* **Answering questions.** `AskUserQuestion` gets a button per option — one tap for a single
  choice, toggle-then-send for multi-select, several questions collected together, or a free-text
  **reply** to the card. Claude Code has no hook output that supplies a tool *result*, so the
  answer rides back on `PreToolUse` returning `deny` with the choice in
  `permissionDecisionReason`, which the model reads and continues from. `--question-timeout`
  (120 s) then falls back to the terminal picker.
* Both waits hold the terminal prompt while they wait — a real lockout for anyone at the keyboard,
  which is why both are bounded and configurable. `--no-permission-prompts` turns both off.
* Nothing is ever auto-approved or auto-answered, no `--dangerously-skip-permissions` is added,
  and no permission mode is changed.

### Driving the session

* `/stop` interrupts the running turn (the agent's own Escape).
* Agent slash commands work from the chat — `/compact`, `/clear`, `/context`, `/usage`,
  `/model <name>`, `/effort <level>`, `/mcp`, `/config` and friends. `/exit` is excluded: it would
  end the session in the tmux seat and take the remote side with it.
* The bridge starts itself when mirroring is enabled, and refuses to start a second poller for the
  same bot token.

### Staying legible

* Markdown is rendered to Telegram HTML, with a plain-text fallback on any rejection so formatting
  is never traded for content.
* Blocking dialogs (`AskUserQuestion`, MCP `Elicitation`) stop the typing indicator and say what
  the session is waiting for, instead of showing "typing…" beside a session that is really parked
  on a picker.
* `PreCompact`/`PostCompact` explain the gap `/compact` leaves.
* A connect-time recap digests the last few exchanges so a phone joining mid-session has context.
  This is the project's only transcript read: bounded, once, on an explicit action, `--no-recap`
  to disable.
* Several sessions can share one bridge. Each keeps its own streaming state — one session's turn
  end no longer finalizes another's half-written message — and messages gain a `[project]` label
  once more than one is connected.

### Installing

* `agent2telegram remote-control install|uninstall|doctor` — merges hook entries into
  `settings.json` without touching anyone else's, backs the file up first, is idempotent, and
  reports exactly what changed.
* [`skills/local-remote-setup`](skills/local-remote-setup/) — an agent playbook so Claude Code can
  do the install itself, with the security rules it must not break.
* `install.sh` no longer hard-codes upstream's clone URL. It installs from the checkout it is run
  from; installing the wrong repository would have produced a bridge with no Remote Control and no
  obvious symptom.

### Upstream files touched

| File | Change |
| --- | --- |
| `attach.py` | consume the spool in the outbound loop; route `callback_query`; typing follows the mirror; `parse_mode` on the durable send path; `/stop` and command passthrough |
| `telegram.py` | inline keyboards, `answerCallbackQuery`, and `edit_plain` reports success so a caller can fall back to plain text |
| `session.py` | `inject_raw` (no origin prefix) and `interrupt` |
| `readers.py` | the Claude tool summarizer moved to `remote_control.core` so the hook path shares it |
| `__main__.py` | the `remote-control` command |
