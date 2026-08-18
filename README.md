# Local Claude Code Remote Control (via Telegram)

Continue a **local, interactive Claude Code session** from Telegram — including sessions whose
model backend is *not* the Anthropic API, such as a local model served through
[Claude Code Router (CCR)](https://github.com/musistudio/claude-code-router) or any other LLM
gateway or proxy.

> **Unofficial.** This is not Anthropic's Remote Control, and this project is not affiliated
> with Anthropic, Alibaba/Qwen, Claude Code Router or Telegram. It is a fork of
> **[Agent2Telegram](https://github.com/petrludwig-collab/Agent2Telegram)** by
> petrludwig-collab (MIT), which supplies the entire Telegram transport. Upstream's own README
> is kept verbatim at [`docs/UPSTREAM_README.md`](docs/UPSTREAM_README.md).

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

---

## The problem this solves

Claude Code ships [Remote Control](https://code.claude.com/docs/en/remote-control), which pairs
a local session with claude.ai and your phone. It requires the session to talk to
`api.anthropic.com` directly: as of v2.1.196 it is **disabled whenever `ANTHROPIC_BASE_URL`
points at another host**, and it is unavailable on Amazon Bedrock, Google Cloud's Agent Platform
and Microsoft Foundry.

That rules out an entire class of local harness — Claude Code as the agent loop, a local model
(Qwen, Llama, …) as the brain, a gateway in between:

```
Claude Code  →  CCR gateway  →  local model
```

Everything else about that setup is worth keeping: the tools, the permissions, the skills, the
subagents, the filesystem access. Only the *remote surface* is missing.

This project rebuilds that remote surface out of **documented Claude Code hooks** and sends it
to Telegram.

## Do I need CCR?

**No.** Nothing in the code knows or cares what the model backend is — it only needs Claude Code
running in a tmux session, plus its hooks. CCR is simply the thing that *created* the need, by
disabling native Remote Control.

Native Remote Control is unavailable — and this is useful — whenever any of these is true:

* you authenticate with an **API key** rather than a Pro/Max/Team/Enterprise subscription;
* you are on **Amazon Bedrock**, **Google Cloud's Agent Platform** or **Microsoft Foundry**;
* `ANTHROPIC_BASE_URL` points at an **LLM gateway or proxy** — CCR, LiteLLM, your own;
* or you just want the session in **Telegram** rather than claude.ai and the Claude app.

On an ordinary subscription-backed setup, native Remote Control is the better tool and you should
use it. This exists for the cases where you can't.

## What you get

Enable it for a session and your phone shows the local seat, live:

| Locally | In Telegram |
| --- | --- |
| you type a prompt | `🖥️ You: investigate why pending transactions are duplicated` |
| the model starts answering | the text appears and **grows as it streams** |
| it reads a file | a temporary status bubble: `📄 Reading transactions.ts` |
| it edits, then runs tests | the same bubble updates: `✏️ Editing reconcile.ts` → `🛠️ Run the test suite` |
| a subagent or task runs | `🤖 Explore running`, `📋 Working: reconcile pending transactions` |
| the turn ends | the bubble disappears; the answer stays |
| the API errors out | `⚠️ Turn ended with an error (overloaded)` |
| a tool needs permission | `🔐 Permission needed` with **✅ Allow / ⛔ Deny** buttons — press one and the session continues |
| it asks you a question | `❓ Waiting for your answer` with a **button per option** — tap one, or reply with your own answer |
| the context is compacted | `🗜️ Conversation compacted (auto)`, so the gap is explained rather than mysterious |
| you send `/stop` | the turn is interrupted |
| you send `/compact`, `/model sonnet`, … | the command runs in the session |

It works in both directions: messages you send **from** Telegram drive the same live tmux
session through Agent2Telegram's existing attach mode, exactly as before.

## Key properties

* **Claude Code and your tools stay local.** Nothing about the agent moves off the machine;
  Telegram is only a display and an input.
* **No inbound ports, no webhook, no web server.** Telegram long polling only, so it works
  behind NAT and a strict firewall.
* **Per-session opt-in.** Mirroring is off until you run the Skill in that specific session.
  A normal session stays entirely local.
* **`/compact` and `/clear` keep it connected**; a fresh `startup`, `resume` or `fork` starts
  disconnected — the same rule native Remote Control uses.
* **No terminal scraping and no transcript parsing** on the local-mirror path. Assistant text
  comes from the documented `MessageDisplay` hook.
* **Hooks do no network I/O.** They write one small file and exit; the long-running bridge does
  every Telegram call. Two of them wait — a permission request and a question — and they wait on
  a local file, only while Claude Code is already stopped asking a human anyway.
* **Telegram-originated turns are never duplicated.** `UserPromptSubmit` classifies each turn's
  origin, and the mirror only ever handles terminal-originated ones.
* **Permissions and questions are decided by a human, remotely or locally.** Both go to the chat
  with buttons; if nobody answers in time, the normal terminal prompt or picker appears. Nothing
  is ever auto-approved or auto-answered.
* **The bridge starts itself.** Enabling mirroring launches the bridge if it isn't running —
  and refuses to start a second one, because Telegram allows exactly one poller per bot.
* **It doesn't buzz at you.** Progress is delivered silently — it arrives and stays in the chat,
  it just makes no sound. You get a real notification only when it needs a decision, hits an
  error, or finishes. `--loud` restores a ping per message.
* **A blocked session never looks busy.** When Claude Code stops to ask, typing stops and the
  chat shows the question — with its options, ready to answer.
* **Several sessions can share one bridge.** Each keeps its own streaming state, and messages
  gain a `[project]` label as soon as more than one is connected.
* **Qwen through CCR is the reference configuration** — see
  [`docs/QWEN_CCR_SETUP.md`](docs/QWEN_CCR_SETUP.md). Nothing in the package is Qwen-specific.

### Approving permissions from your phone

When Claude Code needs permission, the chat gets a card naming the tool and showing a redacted
one-line detail, with **✅ Allow** and **⛔ Deny** buttons:

```
🔐 Permission needed

Bash
🛠️ Remove the build directory
rm -rf ./build
```

Press one and Claude Code continues immediately. The mechanics:

* only [allow-listed](docs/SECURITY.md) Telegram users can press the buttons — the same people
  who can already send prompts into the session;
* the hook waits up to 90 s (`--permission-timeout`), then gives up and the **normal terminal
  prompt appears** — an unanswered phone never blocks the keyboard for long;
* nothing is auto-approved, no `--dangerously-skip-permissions` is added, and your permission
  mode is never weakened — a `deny` rule in your settings still wins over an Allow press;
* the buttons are retracted once the request is answered, expires, or the turn ends, so a stale
  press can never decide anything;
* prefer the old behaviour? `--no-permission-prompts` makes it a notification again.

### Answering Claude's questions from your phone

When Claude asks something with its own picker (`AskUserQuestion`), the chat gets the question
with one button per option:

```
❓ Waiting for your answer

Approach
Rewrite the importer or patch it?
  • Rewrite
  • Patch

Tap an option, or reply to this message with your own answer.
```

* a single-choice question is answered by **one tap**;
* a multi-select question toggles as you tap, then **📨 Send answer**;
* several questions in one ask are collected together before sending;
* **replying to the card** with free text sends that text as the answer — the equivalent of
  typing your own option instead of picking one;
* nobody answers within `--question-timeout` (default 120 s)? The terminal picker appears as
  normal.

How it works is worth knowing, because it constrains what is possible: Claude Code has no hook
output that supplies a tool *result*, so the answer cannot be handed to `AskUserQuestion`
directly. It rides back the documented way instead — the tool call is blocked with
`permissionDecision: "deny"` and your choice goes in `permissionDecisionReason`, which Claude
Code shows to the model, which continues with it and is told not to ask again.

MCP elicitations are reported but **not** answerable: they have no equivalent decision channel.

### What you can send to the session

All of this is upstream Agent2Telegram's, and it works here unchanged:

| You send | What happens |
| --- | --- |
| **text** | goes straight to the live session as a prompt |
| **a photo** | downloaded to the machine and handed to the agent as a file reference |
| **a file** | same — up to Telegram's 20 MB bot limit, which it tells you about if you exceed it |
| **a voice note** | transcribed, then sent as text — **off by default**, see below |
| **a ❤️ reaction** | delivered to the agent as quick feedback, no reply expected |
| **a reply to a question card** | taken as your answer to that question |

#### Voice notes (optional)

Voice messages are transcribed with **ElevenLabs Scribe** (`scribe_v1`) using **your own** API
key — there is no shared key, no extra Python dependency, and it is off until you add one:

```bash
export ELEVENLABS_API_KEY="sk_..."      # or set "elevenlabs_api_key" in config.json
```

or from the chat, `/setkey <your-key>` — which then **deletes your message**, so the key is not
left sitting in the conversation. Without a key, a voice note gets a short "not enabled" notice.
Photos and files need no setup.

Note that this uploads the audio to ElevenLabs, a third party; see
[`docs/SECURITY.md`](docs/SECURITY.md).

### Driving the session from Telegram

Beyond sending prompts:

| Command | Effect |
| --- | --- |
| `/start`, `/help` | what you can send |
| `/status` | which agent and tmux session you're driving, and whether voice is on |
| `/id` | your Telegram id, for the allow-list |
| `/setkey <key>` | enable voice transcription (your message is deleted afterwards) |
| `/stop` | Interrupt the running turn (the agent's own Escape, not a kill) |
| `/compact`, `/clear`, `/context`, `/usage`, `/recap` | Run in the session |
| `/model sonnet`, `/effort high`, `/fast`, `/color`, `/rename` | Run with the argument |
| `/mcp`, `/config`, `/autocompact`, `/reload-plugins` | Run in the session |

The first four are answered by the bridge itself; the rest are forwarded to the agent verbatim,
because it only treats a line as a command when `/` is the
very first character — which is why the `[TG] ` origin prefix is recorded out of band for them
instead. `/exit` is deliberately not forwarded: it would end the session in the tmux seat and
take the remote side down with it. Anything else starting with `/` is passed to the agent as
ordinary text, as before.

### Also inherited from Agent2Telegram

Documented in full in [`docs/UPSTREAM_README.md`](docs/UPSTREAM_README.md), and easy to miss
because this README is about the fork:

| | |
| --- | --- |
| `agent2telegram notify "build finished ✅"` | push a message to yourself from a cron job or a background script — the supported way for something *outside* a turn to reach you |
| `agent2telegram service` | prints a systemd or launchd unit, so the bridge survives a reboot |
| `agent2telegram selftest --agent claude-code` | end-to-end test against a real agent in a throwaway tmux session, with a fake Telegram — no bot, no chat touched |
| `agent2telegram doctor` | checks the bridge config and the token, with the token redacted |
| `agent2telegram uninstall` | removes the bridge itself (`remote-control uninstall` removes only this fork's part) |
| `Dockerfile` | container image for the bridge; the agent CLI and its login are not baked in |

Long replies are split at Telegram's 4096-character limit on paragraph, line or word boundaries;
flood control (`429`), transient network errors and Markdown parse failures are all handled, and
a reply whose send hard-fails is queued to disk and retried until Telegram confirms it.

### Notifications

A long turn is a lot of messages, and Telegram buzzes once per *new* message (edits never
notify at all). Twelve assistant messages used to mean twelve buzzes, so the default now splits
delivery from notification:

| | |
| --- | --- |
| **silent** — arrives, no sound | your prompt echo, streamed assistant text, the tool bubble, the connect recap, compaction notices |
| **notifies** | a permission card, a question, an MCP elicitation, a turn failure, an actionable notification, and one line when the turn ends |

Nothing is withheld — the chat history is identical either way, this only controls the sound.
The end-of-turn line previews the answer (`✅ Done — All 22 detector tests pass`) so the lock
screen is useful, and it is skipped when the turn produced no output or when the full answer was
delivered at the end anyway.

Pass `--loud` at install or connect time to go back to a notification per message.

### Connecting mid-session

Enabling mirroring sends a short digest of the last few exchanges, so a phone joining an
in-flight session has context instead of a stream that starts mid-thought. This is the only
place the project reads a transcript — once, on your explicit action. `--no-recap` turns it off.

---

## How it works

```
        local model  ←  CCR gateway  ←  Claude Code  ─── documented hooks ───┐
                                            │                               │
                                            │                        remote-control
                          Telegram ─── tmux send-keys ───┐              hook adapter
                                                         │                  │
                                                         │           fast local spool
                                                         │                  │
                                                  Agent2Telegram AttachBridge
                                                                  │
                                                              Telegram
```

* **Terminal → Telegram** (new): Claude Code hooks → `agent2telegram.remote_control.core` writes
  a small JSON event into a private spool → the bridge drains the spool and streams to Telegram.
* **Telegram → Claude Code** (unchanged upstream behaviour): the bridge injects the message into
  the live tmux session with an `[TG]` origin prefix and forwards the reply from the transcript.

Full event-by-event walkthrough: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Requirements

* Claude Code (`claude`) — the hook events used here need a recent version (`MessageDisplay`,
  `SubagentStart`, `TaskCreated`/`TaskCompleted`, `StopFailure`, `PreCompact`/`PostCompact`,
  `Elicitation`). Everything degrades to "that part isn't mirrored" if an event is missing, never
  to a broken session.
* One caveat worth stating plainly: Claude Code's hook reference does not mention
  `AskUserQuestion`, so *answering questions remotely relies on `PreToolUse` firing for it* —
  which follows from it being a tool, but is an inference rather than a documented guarantee. If
  it doesn't fire on your version, you simply get today's behaviour: the picker appears at the
  terminal and nothing is lost.
* Python 3.10+ — the runtime has **zero** third-party dependencies.
* `tmux` — hosts the session the bridge attaches to.
* A Telegram bot token and your numeric Telegram user id.

## Install

### Let Claude Code do it

This repo ships an agent playbook. Copy it in, then ask:

```bash
git clone https://github.com/pwillia7/local-claude-code-remote.git
cd local-claude-code-remote
mkdir -p "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
cp -r skills/local-remote-setup "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/"
```

> Set up Local Claude Code Remote Control for me.

It runs the preflight checks, asks for the two things it can't discover (your bot token and your
Telegram id), previews every change with `--dry-run` first, and verifies the result. See
[`skills/`](skills) for what it will and won't do.

### Or by hand

```bash
python3 -m pip install --user .          # or: pip install --user -e . while developing

# 1. one-time Agent2Telegram setup (bot token, your user id, attach mode + tmux session)
python3 -m agent2telegram setup

# 2. install the Remote Control Skill and merge the hooks into Claude Code's settings.json
python3 -m agent2telegram remote-control install \
    --claude-config-dir "$CLAUDE_CONFIG_DIR" \
    --tmux-session <tmux-session> \
    --skill-name local-remote \
    --label "Local Remote Control"

# 3. check the whole chain
python3 -m agent2telegram remote-control doctor --claude-config-dir "$CLAUDE_CONFIG_DIR"
```

`--claude-config-dir` defaults to `$CLAUDE_CONFIG_DIR`, then `~/.claude`. `--agent2telegram-config`
defaults to `$AGENT2TELEGRAM_CONFIG`, then the config in `~/.config/agent2telegram/` whose
`tmux_session` matches. Add `--dry-run` to see the changes without making them.

The installer:

* verifies Claude Code, the bridge config, `tmux` and the Claude config directory;
* installs the Skill with this machine's paths filled in;
* **merges** its hook entries into `settings.json` without touching anyone else's hooks;
* backs the file up first, is idempotent, and reports exactly what it changed.

Then start the bridge (see `python3 -m agent2telegram service` for a systemd/launchd unit):

```bash
python3 -m agent2telegram run
```

Run **exactly one** bridge per bot token — Telegram allows only one long-poll consumer. You do
not have to start it by hand: enabling mirroring starts it for you (and detects an existing one
rather than adding a second).

## Use

In the Claude Code session you want to mirror:

```
/local-remote
```

Telegram gets `🟢 … connected`. Run it again to disconnect. That is the whole interface — the
Skill sets `disable-model-invocation: true`, so the model can never enable mirroring on its own.

```bash
python3 -m agent2telegram remote-control status          # what is mirrored right now
python3 -m agent2telegram remote-control off <session>   # force-disconnect one session
python3 -m agent2telegram remote-control uninstall       # remove hooks, Skill and state
```

## Documentation

| Document | Contents |
| --- | --- |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | event flows, state layout, spool contract, design rationale |
| [`docs/QWEN_CCR_SETUP.md`](docs/QWEN_CCR_SETUP.md) | the reference Qwen + CCR + tmux + Telegram arrangement |
| [`docs/SECURITY.md`](docs/SECURITY.md) | threat model, what is stored, what is never stored |
| [`docs/UPSTREAM_README.md`](docs/UPSTREAM_README.md) | upstream Agent2Telegram's README, verbatim |
| [`skills/`](skills) | the `local-remote-setup` agent playbook — let Claude Code install this for you |
| [`CHANGELOG.md`](CHANGELOG.md) | what this fork added, in order |
| [`examples/`](examples) | sanitized launcher, shell integration and bridge config |

## Compatibility

Exercised on Linux with Claude Code 2.1.x routed through CCR to a local Qwen model, Python 3.11
and 3.14, and Agent2Telegram attach mode. Other gateways, models and platforms should work — the
implementation only depends on documented hook events — but are untested, and this README does
not claim more than has actually been run.

## What it doesn't do

It is worth being precise about this, because the pitch — "Remote Control for gateway-routed
sessions" — invites the assumption that it is a drop-in replacement. It isn't.

**Native does these; this doesn't:**

* **Command output doesn't come back.** `/context`, `/usage`, `/compact` and friends *run* when
  you send them, but their output is drawn in the terminal UI, not written to the transcript or
  any hook. Reading it would mean scraping the pane, which this project deliberately never does —
  so the chat confirms the command was sent, and the result stays on screen.
* **`/resume` disconnects.** Native follows you into the conversation you switch to. Here a
  resumed session starts disconnected, by the same rule as `startup` and `fork`; run the toggle
  again in the resumed session.
* **No session list, titles, `/rename`, session URL or QR code.** One bridge, one chat. Several
  sessions can share it and get a `[project]` label, but there is nothing to browse or name.
* **No `@` file autocomplete** when typing from the phone.
* **No starting a session remotely.** Native server mode spawns sessions on demand, optionally
  each in its own git worktree. Here you attach to a tmux seat that already exists.
* **No presence suppression.** Native skips pushes while you are at the keyboard (via
  `CLAUDE_CLIENT_PRESENCE_FILE`); every message here reaches your phone regardless.
* **MCP elicitations are reported, not answerable** — unlike `AskUserQuestion`, they have no
  decision channel, only a refusal.
* **Files and images Claude produces arrive as text.** Inbound works — see
  [What you can send](#what-you-can-send-to-the-session) — but outbound attachments do not.
* Cross-session messaging, Trusted Devices and organization policy controls: not applicable.

**Deliberate, not missing:**

* Only **terminal-originated** turns are mirrored. Telegram-originated ones keep upstream's
  transcript path, which is what guarantees no answer is ever delivered twice.
* A pending permission or question **holds the terminal prompt** while it waits for your phone.
  That is the cost of answering remotely at all; both waits are bounded and configurable.
* No terminal scraping, and no transcript on the live path.

**Honestly untested:** whether extended thinking appears in `MessageDisplay` (so it may or may not
be mirrored), and the `AskUserQuestion` inference noted under [Requirements](#requirements).

## Relationship to upstream

This repository keeps the full Agent2Telegram git history, its MIT `LICENSE`, and its transport,
retry, flood-control, attach-mode, media, voice and self-test behaviour. Upstream's tests still
run and pass. The changes are additive:

| File | Change |
| --- | --- |
| `agent2telegram/remote_control/` | **new** — hook adapter, spool, mirror, CLI, installer, bridge supervision, Skill template |
| `skills/local-remote-setup/` | **new** — agent playbook for installing and troubleshooting |
| `agent2telegram/attach.py` | consume the spool in the outbound loop; route `callback_query` updates to the mirror; the typing indicator honours the mirror; the durable send path takes a `parse_mode` |
| `agent2telegram/telegram.py` | inline keyboards on send/edit, `answerCallbackQuery`, and `edit_plain` now reports success so a caller can fall back to plain text |
| `agent2telegram/readers.py` | the Claude tool summarizer moved to `remote_control.core` so the hook path shares it |
| `agent2telegram/__main__.py` | the `remote-control` command |
| `tests/test_remote_control.py` | **new** |

Upstream is kept as the `upstream` git remote. Nothing here is pushed to it.

## Future work

Shipped since the first cut: remote permission approval, answering Claude's questions, Markdown
rendering, bridge auto-start, blocking-dialog reporting, `/stop`, agent slash commands,
multi-session support, compaction notices and the connect-time recap. See [`CHANGELOG.md`](CHANGELOG.md).

Still open:

* Answering **MCP elicitations** from the chat — unlike `AskUserQuestion` they have no decision
  channel, only a refusal (exit 2).
* An "always allow this tool" button, once there is a documented way to persist a permission rule.
* Remote permission prompts for **Telegram-originated** turns too; today they are mirrored for the
  local seat only, which is what guarantees no turn is ever duplicated.
* **Presence awareness** — Claude Code skips its own pushes while you are at the keyboard, via
  `CLAUDE_CLIENT_PRESENCE_FILE`. Adopting it is easy; deciding what to suppress is not, because
  our messages are the mirror itself, so skipping them leaves holes in the chat history.
* Multiple CCR profiles/models against one bridge, and a generic Skill alias.
* Transports other than Telegram.
* Upstreaming the generic hook-event adapter to Agent2Telegram.

## Licence

MIT — see [`LICENSE`](LICENSE). The original copyright is retained; this fork adds to it.
