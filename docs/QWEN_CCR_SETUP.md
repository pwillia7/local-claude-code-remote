# Reference setup: local Qwen through CCR, mirrored to Telegram

This is the arrangement the project was built against and tested on. Substitute your own values
for every placeholder — `$HOME`, `<tmux-session>`, `<ccr-profile>`, `<telegram-user-id>`.

```
qwen  (shell function)
  └─ persistent tmux seat  "<tmux-session>"
       └─ $HOME/.local/bin/qwen-direct
            └─ ccr "<ccr-profile>" cli
                 └─ claude   (CLAUDE_CONFIG_DIR = the CCR profile's claude dir)
                      ├─ ANTHROPIC_BASE_URL → CCR gateway → local Qwen
                      └─ hooks → remote-control spool
                                    └─ agent2telegram run  (attach mode)
                                          └─ Telegram
```

Two things drive the same seat: you at the terminal, and Telegram through
`tmux send-keys`. Both see one conversation.

---

## 1. Claude Code Router

CCR keeps a per-profile Claude configuration directory, so this harness's settings and hooks
never touch `~/.claude`:

```
$HOME/.claude-code-router/profiles/<ccr-profile>/claude/
├── settings.json          ← env, hooks, statusLine, permissions
└── skills/<skill-name>/   ← the Remote Control Skill
```

The launched `claude` process runs with:

```
CLAUDE_CONFIG_DIR=$HOME/.claude-code-router/profiles/<ccr-profile>/claude
```

and `settings.json` sets `ANTHROPIC_BASE_URL` to the local CCR gateway. **That is precisely why
native Remote Control is unavailable** — see
[the docs](https://code.claude.com/docs/en/remote-control): it is disabled when
`ANTHROPIC_BASE_URL` points anywhere other than `api.anthropic.com`.

Do not change CCR's model, token or context settings to make this work. It does not need them.

## 2. The launcher

`$HOME/.local/bin/qwen-direct` — see [`examples/qwen-direct`](../examples/qwen-direct):

```bash
#!/usr/bin/env bash
set -euo pipefail
CCR_BIN="${CCR_BIN:-$(command -v ccr)}"
"$CCR_BIN" start --no-open >/dev/null 2>&1 || { echo "Couldn't start CCR." >&2; exit 1; }
exec "$CCR_BIN" "<ccr-profile>" cli -- "$@"
```

## 3. The persistent tmux seat

The `qwen` shell function creates the seat once and re-attaches to it afterwards, so Telegram
always has a live session to talk to — see
[`examples/qwen-zshrc.zsh`](../examples/qwen-zshrc.zsh):

```
qwen()
  if already inside <tmux-session>:  run qwen-direct here
  else:                              create/attach <tmux-session>,
                                     launching qwen-direct in it if idle
```

Keep the seat. The bridge attaches to it; killing it drops the remote side.

## 4. Agent2Telegram in attach mode

```bash
python3 -m agent2telegram setup      # bot token, your user id, attach mode, tmux session
```

The resulting `~/.config/agent2telegram/config.json` looks like
[`examples/agent2telegram-config.json`](../examples/agent2telegram-config.json):

```json
{
  "agent": "claude-code",
  "mode": "attach",
  "tmux_session": "<tmux-session>",
  "origin_prefix": "[TG] ",
  "progress_marker": "[TG]",
  "transcript_path": "auto",
  "signal_file": "$HOME/.local/state/agent2telegram/answer.txt",
  "allowed_user_ids": ["<telegram-user-id>"],
  "token": "<from @BotFather — keep it out of git>"
}
```

The file holds a secret. It is written `0600`, and it must never be committed.

Run exactly one bridge for the token:

```bash
python3 -m agent2telegram run            # or the unit from: python3 -m agent2telegram service
```

You can skip this: `/qwen-remote` starts the bridge itself when nothing is consuming, in a tmux
session called `a2t-<tmux-session>`. It will never start a second one — if a bridge is already
running but not draining the spool (an older build, or a wedged one), it says so instead.

## 5. Install Remote Control

```bash
python3 -m agent2telegram remote-control install \
    --claude-config-dir "$HOME/.claude-code-router/profiles/<ccr-profile>/claude" \
    --tmux-session "<tmux-session>" \
    --skill-name qwen-remote \
    --label "Qwen Remote Control"
```

This merges the hook entries into that profile's `settings.json` (backing it up first, leaving
every other hook alone) and writes the Skill to
`.../claude/skills/qwen-remote/`. Use `--dry-run` first if you want to see the diff.

Two options worth knowing:

* `--permission-timeout <seconds>` (default 90) — how long a permission request waits for an
  Allow/Deny press before the terminal prompt takes over. The `PermissionRequest` hook's own
  `timeout` in `settings.json` is set 30 s above it, so our graceful fallback always wins.
* `--question-timeout <seconds>` (default 120) — the same, for a question Claude asks with its
  own picker, which the chat can answer by tapping an option or replying with text.
* `--no-permission-prompts` — turn off both remote approvals and remote answers; both are then
  reported only, and decided at the keyboard.

Both timeouts hold the terminal prompt while they wait, so pick them for how often you are
actually at this machine.

Verify:

```bash
python3 -m agent2telegram remote-control doctor \
    --claude-config-dir "$HOME/.claude-code-router/profiles/<ccr-profile>/claude" \
    --skill-name qwen-remote
jq empty "$HOME/.claude-code-router/profiles/<ccr-profile>/claude/settings.json"
```

and, inside a session, `/hooks` lists what Claude Code actually loaded.

## 6. Use it

```
qwen                # attach to the seat
/qwen-remote        # toggle mirroring for THIS session
```

Telegram shows `🟢 Qwen Remote Control connected`, followed by a digest of where the session is
up to. From then on, everything you do at the terminal appears there until you run
`/qwen-remote` again, exit, resume or fork.

From the chat you can also send `/stop` to interrupt a turn, and agent commands like `/compact`,
`/context` or `/model <name>`.

## After a reboot

Worth knowing which links come back on their own and which you bring back by hand:

| Link | After a reboot |
| --- | --- |
| the model endpoint (local server, or a forward to another host) | **out of scope for this project** — see below |
| CCR | started by the launcher (`ccr start --no-open` is idempotent) |
| the tmux seat + Claude Code | **you**, by running `qwen` |
| the Agent2Telegram bridge | started automatically the first time you run `/qwen-remote` |
| hooks, Skill, package, state | on disk, nothing to do |

So the normal sequence after a restart is just `qwen`, then `/qwen-remote` in the session. The
bridge does not need starting by hand.

**The one gap:** the bridge starts *on connect*. Until you run the toggle at least once, nothing
is polling Telegram, so a message you send from your phone before connecting is not received —
Telegram queues it and it arrives once a bridge starts, but the session will not act on it in the
meantime. If you want the phone to work without touching the terminal first, install the bridge
as a boot service:

```bash
python3 -m agent2telegram service      # prints a systemd/launchd unit to install
```

That is upstream's model (always-on) and it composes fine with the auto-start, which detects a
running bridge and leaves it alone.

**The model endpoint is not this project's business.** A local server, a llama.cpp instance on
another machine reached through a port forward or VPN, a hosted gateway — nothing here knows or
cares. It is worth making yours start at boot in its own right (a systemd unit with
`Restart=always` is the usual answer), because if the model path is down, turns fail at the
*gateway*, and what you see in Telegram is a `StopFailure` notice rather than anything wrong with
the mirror.

## Notes specific to this arrangement

* A local model produces long, bursty text. The mirror throttles Telegram edits to one per
  ~0.6 s per message, so a fast local model cannot trip flood control.
* Local models call tools enthusiastically, so remote approval earns its keep here: without it,
  a turn started from the phone stalls on the first prompt until you get back to the keyboard.
* Local models are also more likely to hit gateway errors. `StopFailure` ends the mirrored turn
  and reports `error_type` — otherwise the phone would sit on "typing…" forever.
* CCR restarts do not affect the mirror: the spool is on disk and the bridge drains it when it
  comes back.
* `/compact` matters more with a small context window, and it deliberately does **not**
  disconnect.
