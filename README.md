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
| a tool needs permission | `🔐 Waiting for permission` — **notification only** |

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
  every Telegram call.
* **Telegram-originated turns are never duplicated.** `UserPromptSubmit` classifies each turn's
  origin, and the mirror only ever handles terminal-originated ones.
* **Qwen through CCR is the reference configuration** — see
  [`docs/QWEN_CCR_SETUP.md`](docs/QWEN_CCR_SETUP.md). Nothing in the package is Qwen-specific.

**Current limitation:** remote permission approval is **notification-only**. You are told that
Claude Code is waiting for a decision; you still make that decision at the terminal. Nothing is
ever auto-approved, no `--dangerously-skip-permissions` is added, and your permission mode is
never weakened.

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
  `SubagentStart`, `TaskCreated`/`TaskCompleted`, `StopFailure`).
* Python 3.10+ — the runtime has **zero** third-party dependencies.
* `tmux` — hosts the session the bridge attaches to.
* A Telegram bot token and your numeric Telegram user id.

## Install

```bash
git clone https://github.com/<you>/local-claude-code-remote.git
cd local-claude-code-remote
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

Run **exactly one** bridge per bot token — Telegram allows only one long-poll consumer.

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
| [`examples/`](examples) | sanitized launcher, shell integration and bridge config |

## Compatibility

Exercised on Linux with Claude Code 2.1.x routed through CCR to a local Qwen model, Python 3.11
and 3.14, and Agent2Telegram attach mode. Other gateways, models and platforms should work — the
implementation only depends on documented hook events — but are untested, and this README does
not claim more than has actually been run.

## Relationship to upstream

This repository keeps the full Agent2Telegram git history, its MIT `LICENSE`, and its transport,
retry, flood-control, attach-mode, media, voice and self-test behaviour. Upstream's tests still
run and pass. The changes are additive:

| File | Change |
| --- | --- |
| `agent2telegram/remote_control/` | **new** — hook adapter, spool, mirror, CLI, installer, Skill template |
| `agent2telegram/attach.py` | consume the spool in the outbound loop; the typing indicator honours the mirror; the durable send path takes a `parse_mode` |
| `agent2telegram/readers.py` | the Claude tool summarizer moved to `remote_control.core` so the hook path shares it |
| `agent2telegram/__main__.py` | the `remote-control` command |
| `tests/test_remote_control.py` | **new** |

Upstream is kept as the `upstream` git remote. Nothing here is pushed to it.

## Future work

* Telegram inline **Allow/Deny** buttons for `PermissionRequest`.
* Multiple CCR profiles/models against one bridge, and a generic Skill alias.
* Transports other than Telegram.
* Upstreaming the generic hook-event adapter to Agent2Telegram.
* Richer reconnection/session status in the chat.

## Licence

MIT — see [`LICENSE`](LICENSE). The original copyright is retained; this fork adds to it.
