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
| Claude transcript tailing | Written per *record*, not per *delta* — it cannot stream, and it has no documented contract for partial text. Upstream uses it for the **Telegram-originated** path, which only needs completed messages. |
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
| `PreToolUse` | Tool status bubble. |
| `PostToolUseFailure` | Tool failure in the bubble. |
| `PermissionRequest` | Notify that a decision is pending (never decide). |
| `Notification` | `idle_prompt`, `agent_needs_input`, `agent_completed` only. `permission_prompt` is skipped — `PermissionRequest` already covers it. |
| `SubagentStart` / `SubagentStop` | Subagent progress in the bubble. |
| `TaskCreated` / `TaskCompleted` | Task progress in the bubble. |
| `Stop` | End the turn; finalize streamed messages; `last_assistant_message` as a **backstop only**. |
| `StopFailure` | End the turn on an API error (`Stop` never fires); report `error_type` / `error_message`. |

`PostToolUse` is deliberately **not** registered: the bubble already shows the call, and the
event carries `tool_output`, which is both large and the most likely place for a secret to be.

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

### 4. Turn end

```
Stop → {turn_end}
  ├─ finalize every open MessageDisplay buffer (flush, close)
  ├─ if nothing reached the chat this turn: send last_assistant_message
  ├─ clear the status bubble
  └─ stop the typing indicator
```

The old "🖥️ Qwen finished + entire answer" message is gone: text arrives while the model works,
and `Stop` adds nothing when it already did.

### 5. Failure

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
    └── consumer_heartbeat                0600  bridge liveness
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

**Why plain text while streaming.** A partially written Markdown span (an unclosed `**` or
fence) makes Telegram reject the edit, and `editMessageText` failures are swallowed — so a
rejected edit would silently lose the tail of an answer. Streamed messages therefore stay plain
from first delta to `final`. The `Stop` backstop, which sends a complete message, still renders
Markdown.

**Why the mirror runs in the bridge's outbound loop, not its own thread.** The status bubble,
the durable send queue and the dedup ledger are single-threaded state owned by that loop.
Consuming there means the mirror reuses all of it without a lock.

**Why `_turn_active` was left alone.** That flag drives the Telegram-originated turn, including
its idle fallback and its "never leave a Telegram turn unanswered" backstop. The mirror sets a
separate `_remote_active`; the typing thread honours either.

**Why origin defaults to `terminal`.** A session becomes Telegram-driven only when a prefixed
prompt actually arrives. Anything else — including events before the first prompt — belongs to
the local seat, which is what the user asked to mirror.
