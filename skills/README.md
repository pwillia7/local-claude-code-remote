# Skills

Two Claude Code Skills ship with this project. They do different jobs and are installed
differently.

| Skill | What it is | Installed by |
| --- | --- | --- |
| [`local-remote-setup`](local-remote-setup/) | An **agent playbook** for installing, verifying, repairing and removing Remote Control. Model-invocable: ask Claude Code to set this up and it follows the recipe. | You, by hand (below) |
| `remote_control/skill` (in the package) | The **runtime toggle** — the `/<name>` command that connects and disconnects a session. Deliberately *not* model-invocable, since connecting is the user's decision. | `remote-control install` |

## Installing the setup skill

It has to go in before the installer runs — that is what it is for — so copy it into whichever
Claude Code config directory you want to run the setup from:

```bash
# the session you will run the setup from
mkdir -p "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
cp -r skills/local-remote-setup "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/"
```

Then, in a Claude Code session, just say what you want:

> Set up Local Claude Code Remote Control for my Qwen session.

or invoke it directly with `/local-remote-setup`.

It asks for the two things it cannot discover (your bot token and your numeric Telegram id),
runs the installer in `--dry-run` first so you can see the changes, and refuses to do the
dangerous things — printing the token, emptying the allow-list, or starting a second poller
against the same bot.
