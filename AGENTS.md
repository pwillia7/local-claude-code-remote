# AGENTS.md — install playbook for an AI agent

> This file is written **for an AI coding agent** (Codex, Claude Code…)
> tasked with installing Agent2Telegram on a fresh machine for a non‑technical user.
> Follow it as a recipe. Do **not** improvise around the security rules.

## Your goal
Get the Agent2Telegram bridge running so the user can talk to their agent from Telegram,
then **verify it actually works** and hand the user a short summary.

## Hard rules (do not break)
1. **Never weaken security.** The bridge lets a Telegram user run commands on this machine.
   The `allowed_user_ids` list must contain **only** the owner's real Telegram user id.
   Never set it to empty "to make it work", never add unknown ids, never disable the check.
2. **Never print, paste, or commit the bot token.** It is a secret. It lives only in
   `~/.config/agent2telegram/config.json` (mode 0600) or the `TELEGRAM_BOT_TOKEN` env var.
3. **Do not use `shell=True`** or interpolate user input into shell strings anywhere.
4. If a step needs a secret or a decision only the user can make (token, which agent,
   their Telegram id), **ask the user** — don't guess.

## Prerequisites to check first
Run these and fix what's missing before installing:
- `python3 --version` → must be **3.10+**. If missing, install Python 3.
- `git --version` → needed to fetch the repo. Install if missing.
- The agent CLI to bridge must be installed **and logged in**:
  - Claude Code: `claude --version` and a prior `claude` login.
  - Codex: `codex --version` and a prior `codex` login.
  - Test it works headlessly, e.g. `claude -p "say hi"` or `codex exec "say hi"`.
  If the agent CLI isn't installed/authenticated, **stop and ask the user to do that**
  (it requires an interactive login you can't complete for them).

## Install steps
```bash
git clone https://github.com/petrludwig-collab/Agent2Telegram.git
cd Agent2Telegram
python3 -m pip install --user .          # or: --user --break-system-packages
```

## Configure
Prefer the interactive wizard if the user is present:
```bash
python3 -m agent2telegram setup
```
If you must configure non‑interactively, ask the user for (a) which agent and (b) the bot
token and their Telegram user id, then write `~/.config/agent2telegram/config.json`:
```json
{ "agent": "codex", "token": "<ASK THE USER>", "allowed_user_ids": [<ASK THE USER>] }
```
…and `chmod 600` it. To get the user's id: have them message the bot, then read it from
`getUpdates`, or ask them to send `/id` to the bot once it's running.

**Attachments & voice:** images and files work out of the box (they're downloaded and handed
to the agent). Voice transcription is optional — ask the user if they want it; if yes, add
their **own** ElevenLabs key as `elevenlabs_api_key` (or `ELEVENLABS_API_KEY`). Never use a
shared/hardcoded key.

## Verify (do not skip)
```bash
python3 -m agent2telegram doctor
```
Expected: config prints (token redacted), `agent '<name>': ✓ binary found`,
`telegram: ✓ @<botname>`, and a non‑empty `allowed_user_ids`. Fix anything that isn't ✓.

Then start it and confirm a real round‑trip:
```bash
python3 -m agent2telegram run        # leave running; ask the user to message the bot
```
Ask the user to send the bot a message and confirm they get a reply. Only then is it done.

## Make it persistent
```bash
python3 -m agent2telegram service     # prints a systemd/launchd unit + install hints
```
Install it per the printed hints so the bridge starts on boot and restarts on crash.

## Common failures → fixes
| Symptom | Cause | Fix |
|---|---|---|
| `doctor`: `binary NOT found` | agent CLI not on PATH | install it / fix PATH; re‑login |
| `telegram: ✗ ... Unauthorized` | wrong/typo'd token | re‑enter the token from @BotFather |
| Bot replies "not authorized" to the owner | wrong id in allow‑list | put the id from `/id` into `allowed_user_ids` |
| `Agent error: ... timed out` | agent run > `agent_timeout` | raise `agent_timeout` in config |
| Agent's flags differ from defaults | newer/older CLI | set a custom `command`/`continue_command` (use `{prompt}`) |
| `pip install` blocked (PEP 668) | system‑managed Python | add `--break-system-packages` or use a venv |

## When done, tell the user
- which agent is connected, the bot's @username,
- that **only their** Telegram account can use it,
- how to start a fresh conversation (`/reset`) and check status (`/status`).

---

## This fork: local Remote Control

This repository is a fork of Agent2Telegram that adds `agent2telegram/remote_control/` — a
hook-driven mirror of a **local, interactive** Claude Code session, for harnesses whose model
backend is an LLM gateway (CCR, a proxy) rather than `api.anthropic.com`, where native Claude
Code Remote Control is disabled. Read `docs/ARCHITECTURE.md` before changing it.

Extra hard rules for that layer:

1. **The hook path stays fast and local.** `remote_control/core.py` is executed once per hook
   event (including once per `MessageDisplay` delta). It must stay standard-library only, keep
   its imports cheap, and never do network I/O, subprocesses or transcript scanning. It also
   must have **no relative imports** — it is registered as a plain script run with
   `python3 -S -E`, which is what keeps start-up under ~20 ms.
   The single exception is `PermissionRequest`, which waits on a local decision file: there
   Claude Code is already stopped asking a human, so the wait costs nothing that was not being
   spent anyway. Do not add a second waiting event.
2. **Fail open.** Any error in mirroring exits 0. A broken mirror must never break Claude Code.
3. **Never mirror a Telegram-originated turn.** `UserPromptSubmit` records the origin; the
   attach path already owns those turns and a second copy is a user-visible bug.
4. **Never auto-approve a permission.** A decision comes from a person pressing a button or it
   does not come at all: no timers, no heuristics, no model. Check the presser against
   `allowed_user_ids`, honour a request once, and treat a timeout as *no decision* (print
   nothing) rather than consent. Do not add `--dangerously-skip-permissions`, do not change the
   permission mode, do not simulate an approval with tmux keystrokes.
5. **The transcript is not a live source.** The connect-time recap in `core.recap` is the single
   permitted read, it is bounded, and it runs on an explicit user action. Do not add a second
   one, and never put transcript reading on the streaming path.
6. **Never retain event payloads.** A spool file is deleted as soon as it has been applied. Do
   not log message content — log types, counts and ids only.
7. **Never start a second Telegram poller.** Telegram gives each update to exactly one
   `getUpdates` consumer, so two bridges make messages disappear at random. When in doubt about
   whether one is running, do nothing and say so — see `remote_control/supervise.py`.
8. **The installer is additive.** It merges its own hook entries into `settings.json`, backs the
   file up first, and must leave every other hook byte-for-byte alone. Keep it idempotent, and
   keep `uninstall` symmetrical with `install`.
9. **No machine-specific paths in the package.** `$HOME`, a tmux session name or a CCR profile
   name belong in configuration, the installer's arguments, or `examples/` — never in source.

Run the whole suite with `python3 -m unittest discover -s tests -v` (stdlib only, no network)
and lint with `python3 -m pyflakes agent2telegram tests`.
