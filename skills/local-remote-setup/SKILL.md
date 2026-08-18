---
name: local-remote-setup
description: Install, verify, repair or remove Local Claude Code Remote Control — the Telegram mirror for a Claude Code session running through CCR or another LLM gateway. Use when the user wants to set it up, connect Telegram to Claude Code, fix "/qwen-remote does nothing", diagnose a bridge that isn't mirroring, or uninstall it.
---

# Installing Local Claude Code Remote Control

You are setting up a bridge that lets a **Telegram** chat see and drive a **local Claude Code
session**. Work through this as a recipe. Ask the user for anything only they can supply; do not
guess, and do not improvise around the security rules.

## Hard rules — do not break these

1. **Never print, echo, paste or commit the Telegram bot token.** It lives only in
   `~/.config/agent2telegram/config.json` (mode `0600`) or `$TELEGRAM_BOT_TOKEN`. When you need
   to check it, check a *property* of it (does the file parse, does `getMe` succeed) — never its
   value. If you must show config, use `agent2telegram doctor`, which redacts.
2. **`allowed_user_ids` must contain only the owner's real Telegram id.** Anyone on that list can
   run code on this machine, approve tool permissions, and answer Claude's questions. Never empty
   it "to make it work", never add an id the user didn't give you.
3. **Exactly one bridge may poll one bot token.** Telegram delivers each update to a single
   `getUpdates` consumer; two bridges make messages vanish at random. Check before starting one.
4. **Never weaken permissions.** Do not add `--dangerously-skip-permissions`, do not change the
   permission mode, do not disable hooks to "simplify" anything.
5. **Ask before editing `settings.json` by hand.** The installer merges its own entries and backs
   the file up. Hand-editing is how other people's hooks get lost.

## Step 1 — prerequisites

Check these and report what's missing before installing anything:

```bash
python3 --version          # must be 3.10+
claude --version           # Claude Code, installed and logged in
tmux -V                    # required: the bridge attaches to a tmux session
```

If Claude Code isn't installed or logged in, **stop and ask the user to do it** — it needs an
interactive login you cannot complete for them.

Ask the user two things you cannot discover:

* the **bot token** from [@BotFather](https://t.me/BotFather) (they should paste it into the
  wizard themselves in step 3, not to you);
* their **numeric Telegram user id** — the bot's `/id` command reports it, or [@userinfobot](https://t.me/userinfobot).

## Step 2 — install the package

```bash
cd <repo>
python3 -m pip install --user .
python3 -c 'import agent2telegram, sys; print(agent2telegram.__file__)'
```

If pip refuses with an "externally managed environment" (PEP 668) error, the options in order of
preference are a virtualenv, `pipx`, or `--break-system-packages`. Tell the user which you used.

Confirm the import resolves to the installed copy, not the repo working directory — run the check
from `/tmp` so the current directory can't shadow it.

## Step 3 — connect a bot and a session

```bash
python3 -m agent2telegram setup
```

The wizard asks for the agent (**claude-code**), the tmux session, and the token. The session must
be **attach mode** against a tmux session that stays alive — that is the seat both the terminal
and Telegram drive. If the user doesn't have one yet, the reference layout is in
`docs/QWEN_CCR_SETUP.md` and `examples/`.

Verify without revealing the secret:

```bash
python3 -m agent2telegram doctor
```

It prints a redacted config and a `telegram: ✓ @botname` line if the token works.

## Step 4 — install the hooks and the Skill

```bash
python3 -m agent2telegram remote-control install \
    --claude-config-dir "$CLAUDE_CONFIG_DIR" \
    --tmux-session <session> \
    --skill-name local-remote \
    --label "Local Remote Control" \
    --dry-run
```

**Run `--dry-run` first and show the user what it will change.** Then run it for real.

`--claude-config-dir` matters: a harness routed through CCR keeps its own Claude config directory
per profile (`~/.claude-code-router/profiles/<name>/claude`), and the hooks must go **there**, not
in `~/.claude`. Check what the target session actually uses before choosing.

The installer merges its entries into `settings.json`, backs it up first, and leaves every other
hook alone. It is idempotent — running it twice is safe.

Useful options:

| Option | When |
| --- | --- |
| `--no-permission-prompts` | The user does not want to approve tool calls or answer questions from the chat. |
| `--permission-timeout <s>` | How long a permission request waits for a remote answer (default 90). |
| `--question-timeout <s>` | How long a question waits (default 120). |
| `--skill-name <name>` | The command becomes `/<name>`. |

Both timeouts are a **lockout for whoever is at the keyboard**, because the hook holds the prompt
while it waits. Lower them if the user is usually at the machine; raise them if they are usually
not.

## Step 5 — verify

```bash
python3 -m agent2telegram remote-control doctor --claude-config-dir "$CLAUDE_CONFIG_DIR"
```

Every line should be `✓`. Then, in the Claude Code session itself, `/hooks` lists what Claude Code
actually loaded — confirm the Remote Control entries are there and that **the user's existing
hooks still are too**.

Finally, ask the user to run `/local-remote` in their session and confirm Telegram shows
`🟢 … connected`. If they get `REMOTE_ENABLED_WITH_WARNING`, the state was set but the notice
couldn't be delivered — check the bridge.

## Troubleshooting

**Nothing appears in Telegram.** Check `remote-control status` and `remote-control doctor`. The
usual causes, in order: mirroring was never enabled for *that* session (it is per-session, and a
fresh `startup`/`resume`/`fork` resets it to off); no bridge is consuming; the hooks went into the
wrong config directory.

**"a bridge process is running but is not draining the spool."** An older build, or a wedged one.
Find it (`ps -eo pid,args | grep 'agent2telegram run'`), stop that one, and let
`/local-remote` start a fresh one. **Do not start a second bridge alongside it.**

**Messages arrive twice.** Almost certainly two bridges polling the same token. Stop all of them
and start one.

**Telegram messages don't reach the session.** The tmux session named in the config must exist and
have Claude Code running in it: `tmux ls`, then `tmux capture-pane -p -t <session> | tail`.

**The phone says "typing…" forever.** The session is probably blocked on a question or a
permission at the terminal. Newer builds report both; if the user is on an older one, upgrade.

**A hook is slow or erroring.** The hook adapter fails open — it exits 0 on any internal error, so
Claude Code is never blocked by it. Check `~/.local/state/agent2telegram/run.log` for the bridge
side; the hook side is silent by design (it never logs message content).

## Removing it

```bash
python3 -m agent2telegram remote-control uninstall --claude-config-dir "$CLAUDE_CONFIG_DIR"
```

Removes only this project's hooks, its Skill and its state. Unrelated hooks and the Agent2Telegram
config are left alone. Add `--keep-state` to keep the state directory.

## What to tell the user when you're done

* which config directory the hooks went into, and that a backup was made;
* the command that toggles mirroring (`/<skill-name>`), and that it is **per session**;
* that a fresh start, resume or fork begins disconnected, while `/compact` and `/clear` do not;
* whether remote permission approval and question answering are on, and the timeouts;
* that the bot's allow-list is the security boundary — anyone on it can drive the machine.
