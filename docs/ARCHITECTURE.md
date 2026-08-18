# Architecture

Two independent paths share one live Claude Code session running inside a tmux seat.

```
                                   local model
                                        ▲
                                        │
                                  LLM gateway (CCR)
                                        ▲
                                        │
   ┌────────────────────────────── Claude Code ──────────────────────────────┐
   │                                    │                                    │
   │                       documented hook events                            │
   │                                    ▼                                    │
   │                      remote_control hook adapter                        │
   │                    (agent2telegram/remote_control/core.py)              │
   │                                    │                                    │
   │                     atomic writes → private spool                       │
   └────────────────────────────────────┼────────────────────────────────────┘
                                        │
                            Agent2Telegram AttachBridge
                     (outbound loop: drain spool + tail transcript)
                                        │
                                    Telegram
                                        │
                       tmux send-keys ◄─┘  (Telegram → Claude Code)
```

The hook side never talks to the network. The bridge side never reads a hook.

---

## Why hooks instead of the transcript or the terminal

| Source | Rejected because |
| --- | --- |
| `capture-pane` scraping | ANSI/TUI chrome, reflow, and no stable identity for a message. |
| Claude transcript tailing | Written per *record*, not per *delta* — it cannot stream, and it has no documented contract for partial text. Upstream uses it for the **Telegram-originated** path, which only needs completed messages. This project reads it in exactly one place: the connect-time recap (below), once, on an explicit user action. |
| Native Remote Control | [Documented](https://code.claude.com/docs/en/remote-control) as disabled when `ANTHROPIC_BASE_URL` is not `api.anthropic.com` — which is exactly this harness. |

`MessageDisplay` is the documented, delta-level source of assistant text, with `session_id`,
`turn_id`, `message_id`, `index`, `delta` and `final`. That is what the mirror uses.

---

## Hook events used

| Event | Used for |
| --- | --- |
| `SessionStart` | `startup`/`resume`/`fork` → disconnect. `clear`/`compact` → keep the connection, reset the turn's origin. |
| `SessionEnd` | Real exits (`logout`, `prompt_input_exit`, …) clean up. `clear`/`resume` deliberately do **not** — they are followed by a `SessionStart`. |
| `UserPromptSubmit` | Classify the turn's origin from `user_input`, and mirror terminal prompts. |
| `MessageDisplay` | Stream assistant text. |
| `PreToolUse` | Tool status bubble — except for `AskUserQuestion`, which is a *blocking dialog*, not activity. |
| `PostToolUse` | Registered with a matcher for blocking tools **only**: it tells us the human answered. `tool_output` is never read. |
| `PostToolUseFailure` | Tool failure in the bubble. |
| `PermissionRequest` | Ask the chat to decide (Allow/Deny buttons) and **block** on the answer; fall back to the terminal prompt if nobody presses one. |
| `Notification` | `idle_prompt`, `agent_needs_input`, `agent_completed` only. `permission_prompt` is skipped — `PermissionRequest` already covers it. |
| `SubagentStart` / `SubagentStop` | Subagent progress in the bubble. |
| `PreCompact` / `PostCompact` | Explain the gap `/compact` leaves in the remote conversation. |
| `Elicitation` / `ElicitationResult` | An MCP server is asking for input, and when it stops. |
| `TaskCreated` / `TaskCompleted` | Task progress in the bubble. |
| `Stop` | End the turn; finalize streamed messages; `last_assistant_message` as a **backstop only**. |
| `StopFailure` | End the turn on an API error (`Stop` never fires); report `error_type` / `error_message`. |

`PermissionRequest` is the single event where the hook deliberately *waits*. It is not a
contradiction of the "hooks return immediately" rule but the reason for it: everywhere else the
hook is on Claude Code's critical path, and here Claude Code is already stopped waiting for a
human. Registered with a `timeout` of the wait plus 30 s, so our own graceful fallback fires
first rather than Claude Code killing the hook mid-wait.

`PostToolUse` is registered **only** with a matcher for `AskUserQuestion` (`EVENT_MATCHERS`). It
fires for every tool otherwise, and carries `tool_output`, which is both large and the likeliest
place for a secret to appear — so it is scoped to the one thing we need from it: knowing that a
blocking question was answered. The mirror never reads the output field.

`Stop` and `StopFailure` additionally run upstream's own `agent2telegram.stop_hook`, which writes
the bridge's `turn_end` marker. That keeps the **Telegram-originated** path's typing indicator
and bubble cleanup working, including on failed turns. The two hooks are registered side by side
rather than wrapped in each other.

---

## Event flows

### 1. Terminal → Claude Code → Telegram (the new path)

```
you type a prompt
  └─ UserPromptSubmit          origin=terminal          → spool {prompt}
model starts answering
  └─ MessageDisplay delta 1    final=false              → spool {message}
  └─ MessageDisplay delta 2…n                           → spool {message}
  └─ MessageDisplay            final=true               → spool {message, final}
model calls a tool
  └─ PreToolUse                                         → spool {tool}
turn ends
  └─ Stop                      last_assistant_message   → spool {turn_end}
```

On the bridge side, one outbound cycle (every 0.4 s):

```
{prompt}   → send "🖥️ You: …"                              (plain text, durable queue)
{message}  → first delta with content: clear the bubble, sendMessage → remember message_id
             later deltas:  append to the buffer, editMessageText at most every 0.6 s
             final:         flush immediately, close, keep in history
{tool}     → status bubble: create once, then edit in place
{turn_end} → finalize open messages, clear the bubble, stop typing,
             and send last_assistant_message ONLY if nothing was streamed
```

Ordering is guaranteed by the spool filename (`time_ns` + pid + randomness, sorted
lexicographically) and by consuming it from a single thread.

### 2. Telegram → Claude Code (unchanged upstream path)

```
Telegram message
  └─ AttachBridge inbound loop (long poll, allow-list check)
      └─ tmux send-keys into the live session, prefixed "[TG] "
          └─ Claude Code runs the turn
              └─ AttachBridge tails the transcript, forwards each assistant message
                  └─ Stop hook writes turn_end → typing off, bubble cleared
```

The mirror sees these turns too, and ignores them: `UserPromptSubmit` saw the `[TG] ` prefix and
recorded `origin=telegram`, so every subsequent hook returns without spooling anything. **A
Telegram-originated answer can never be delivered twice.**

### 3. Tool activity

One temporary bubble, not one message per call:

```
assistant text          → bubble deleted (so the next one lands below the new text)
PreToolUse              → bubble created:  📄 Reading transactions.ts
PreToolUse              → same bubble edited: ✏️ Editing reconcile.ts
SubagentStart/Task*     → same bubble edited
assistant text          → bubble deleted, repositioned below
Stop / StopFailure      → bubble deleted
```

It reuses `AttachBridge._status_push` / `._status_clear`, so there is exactly one status-bubble
implementation and a bridge crash mid-turn still cleans up the orphan on restart.

### 4. Permission approval (terminal-originated turns)

```
PreToolUse …
Claude Code is about to ask for permission
  └─ PermissionRequest hook
       ├─ bridge not consuming?  → return immediately, terminal prompt (never hold the session)
       ├─ spool {permission_request, request_id, tool, redacted detail, timeout}
       └─ poll decisions/<request_id>.json every 100 ms, up to `permission_timeout`

bridge, outbound thread : post a card with an inline keyboard
                          callback_data = "p:<request_id>:a" | "p:<request_id>:d"
bridge, inbound  thread : callback_query arrives
                          ├─ presser not in allowed_user_ids → refuse, buttons stay live
                          ├─ request already answered        → "no longer waiting"
                          └─ write decisions/<request_id>.json, answerCallbackQuery,
                             edit the card to ✅/⛔ and remove the keyboard

hook wakes: prints {"hookSpecificOutput":{"hookEventName":"PermissionRequest",
                                          "decision":"allow"|"deny"}}
```

If the timeout wins instead, the hook prints **nothing** — which the docs define as "the
permission flow proceeds unchanged" — and spools `permission_expired` so the bridge retracts
the buttons. Turn end retracts any card still live, because a press that decides nothing is
worse than no button at all.

Two threads touch the pending-card map (the outbound one posts, the inbound one resolves), so
it is the one piece of mirror state under a lock.

### 5. Blocking dialogs

`AskUserQuestion` and MCP elicitations *stop* the session rather than doing work, and Claude Code
emits no turn end for them. Treating them as ordinary tool activity is what makes a phone show
"typing…" forever next to a session that is really sitting on a picker.

```
PreToolUse(AskUserQuestion)  → spool {question, options, tool_use_id}
   mirror: post a durable card, clear the tool bubble, and STOP the typing indicator
           (working = true, waiting = non-empty ⇒ not typing)
PostToolUse(AskUserQuestion) → spool {question_answered}
   mirror: mark the card answered, resume typing
turn end / interrupt         → any dialog still open is closed as "answered at the terminal"
```

The card cannot be answered from Telegram: the documented hook output for `PermissionRequest` is
a `decision`, and there is no equivalent for supplying an *answer*. Rather than fake one with
tmux keystrokes against a picker, the card reports the question and points at the keyboard.

### 6. Interrupting from Telegram

`/stop` sends the agent's own Escape to the tmux seat — a single press, because a double Escape
opens Claude Code's history rewind. Claude Code fires neither `Stop` nor `StopFailure` for an
interrupt, so the bridge spools an `interrupted` event itself rather than mutating mirror state
from the inbound thread; the mirror ends every turn it is tracking for that seat.

### 7. Turn end

```
Stop → {turn_end}
  ├─ finalize every open MessageDisplay buffer (flush, close)
  ├─ if nothing reached the chat this turn: send last_assistant_message
  ├─ clear the status bubble
  └─ stop the typing indicator
```

The old "🖥️ Qwen finished + entire answer" message is gone: text arrives while the model works,
and `Stop` adds nothing when it already did.

### 8. Failure

```
StopFailure → {turn_failed}
  ├─ finalize whatever was streamed (partial text is kept, not discarded)
  ├─ send "⚠️ Turn ended with an error (<error_type>) … <error_message>"
  ├─ clear the status bubble
  └─ stop typing
```

`Stop` never fires after an API error, so `StopFailure` must terminate the turn on its own. The
Telegram-originated path is covered by the same event through `agent2telegram.stop_hook`.

If *neither* ever arrives — Claude Code was killed, the machine slept — the mirror ends the turn
itself after 180 s of silence, keeping whatever text was already streamed. The chat must never be
left showing a session that is permanently "working".

---

## State layout

Under `$AGENT2TELEGRAM_STATE` (default `~/.local/state/agent2telegram`):

```
remote-control/
├── sessions/<claude-session-id>.json     0600  fast index → bridge, origin prefixes, label
└── <bridge-slug>/                        0700  slug = sanitized tmux session name
    ├── enabled/<claude-session-id>       0600  authoritative "mirroring is on" marker
    ├── origin/<claude-session-id>.json   0600  terminal | telegram
    ├── events/<event-id>.json            0600  the spool
    ├── decisions/<request-id>.json       0600  a remote Allow/Deny, collected by the waiting hook
    ├── consumer_heartbeat                0600  bridge liveness
    └── start.lock                        0600  held only while a bridge is being started
```

The index exists so the hook needs **no tmux call and no config parse**: one `open()` answers
"is this session mirrored, and by which bridge?". For a session that is not mirrored — the
common case — the hook does one failed open and exits.

---

## The spool contract

* **Write**: build a small JSON object → create a `0600` temp file in the same directory →
  `os.replace()` into `events/`. Readers therefore only ever see complete files.
* **Name**: `<time_ns:020d>-<pid:07d>-<8 hex>.json`. Sorting the directory sorts by time.
* **Read**: the bridge lists, sorts, and applies oldest-first, at most 400 per cycle.
* **Acknowledge**: the file is deleted immediately after it is applied — or after the resulting
  outbound message has been handed to the bridge's own durable retry queue. Payloads are never
  retained after forwarding.
* **Corrupt files** are logged and deleted, so one bad file cannot wedge the queue.
* **Bounded**: while the bridge's heartbeat is fresh the spool may grow freely (it survives a
  restart, a network outage and Telegram `429` backoff). Once the heartbeat is more than 10
  minutes stale, writes stop at 500 pending events rather than filling the disk. The consumer
  additionally prunes anything above 1000.

No `fsync`: the spool is a best-effort mirror of a live session, and an `fsync` per text delta
would put milliseconds of disk latency in Claude Code's render path. A machine that loses power
mid-turn loses a few chat lines, not any work.

---

## Cost of the hot path

The registered command is

```
<python> -S -E <site-packages>/agent2telegram/remote_control/core.py
```

`-S -E` skips `site` processing and `PYTHONPATH`, which nearly halves interpreter start-up. It
is safe because `core.py` is standard-library only and has no relative imports, so it runs as a
plain script. `re` is imported lazily — only tool summaries need it, and `MessageDisplay`, the
hottest event, never does.

Measured on the reference machine (50 sequential invocations, wall clock, including the shell
fork): **~22 ms** for a `MessageDisplay` on a mirrored session, **~27 ms** for a `PreToolUse`,
of which ~15 ms is Python interpreter start-up. Going through the CLI
(`python3 -m agent2telegram remote-control hook`) instead costs ~58 ms, which is why the
installer registers the direct form.

The hook does no network I/O, no subprocess, no git, no gateway call, no transcript scan, no
sleep, and holds no lock.

---

## Design decisions worth knowing

**How Markdown survives streaming.** The obvious hazard is a half-written span: if `**bold`
arrives before its closing `**`, a naive renderer emits an unbalanced tag, Telegram rejects the
edit, and — because `editMessageText` failures used to be swallowed — the tail of the answer
disappears. Two things make rendering safe:

* `markdown_to_html` only emits a tag for a *closed* span, so an unterminated `**` or code fence
  stays literal text and the HTML is always balanced;
* every send and edit falls back to the raw text if Telegram rejects the rich version, and
  `edit_plain` now reports success so the mirror can tell. Content is never traded for
  formatting.

Chunking measures the **raw** text (3000 chars) rather than the rendered form, because escaping
only ever makes a message longer — `&` becomes `&amp;` — and the rendered chunk still has to fit
Telegram's 4096. A chunk that somehow renders too long is sent as plain text instead.

**Why the mirror runs in the bridge's outbound loop, not its own thread.** The status bubble,
the durable send queue and the dedup ledger are single-threaded state owned by that loop.
Consuming there means the mirror reuses all of it without a lock.

**Why `_turn_active` was left alone.** That flag drives the Telegram-originated turn, including
its idle fallback and its "never leave a Telegram turn unanswered" backstop. The mirror sets a
separate `_remote_active`; the typing thread honours either.

**Why the bridge starts itself, carefully.** Enabling mirroring is a promise that a phone will
show the session, and "I turned it on and saw nothing" is the worst possible failure. So the
toggle starts the bridge — but Telegram hands each update to exactly one `getUpdates` consumer,
so starting a *second* one makes messages vanish at random. The process list decides whether
anything is running and the heartbeat decides whether it is consuming; a running-but-quiet
bridge is reported, never duplicated; an uninspectable process list falls back to trusting the
heartbeat; and a lock file keeps two concurrent toggles from racing. A fresh heartbeat alone is
not enough evidence — a bridge killed a second ago still has a one-second-old heartbeat.

**Why sessions are tracked separately.** One bridge can mirror several Claude sessions, and the
first cut kept a single `_live` map and a single "delivered" flag for the whole bridge. That is
not merely untidy: session A's `Stop` would finalize session B's half-written message, and B's
permission card would be retracted by A's turn ending. State is per session, the typing indicator
is the OR across sessions, and a `[project]` label appears on messages only once more than one
session is connected — a label on every line would be noise in the normal single-session case.

**Why the recap reads a transcript at all.** Streaming from a transcript is the thing this design
exists to avoid. Reading one *once*, when you connect, is a different operation with a different
failure mode: it is bounded (the last megabyte), it happens on an explicit user action, and if it
returns nothing the only cost is a missing digest. The window is deliberately generous because a
real session's tail is mostly `tool_use`/`tool_result` records with no text — 256 KB of a
tool-heavy transcript yielded a single lonely turn in testing, where 1 MB yielded the full six.

**Why origin defaults to `terminal`.** A session becomes Telegram-driven only when a prefixed
prompt actually arrives. Anything else — including events before the first prompt — belongs to
the local seat, which is what the user asked to mirror.
