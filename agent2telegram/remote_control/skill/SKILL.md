---
name: {{SKILL_NAME}}
description: Toggle Telegram Remote Control for this session ({{LABEL}}).
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/remote.sh *)
---

Toggle Remote Control for this exact session:

!`${CLAUDE_SKILL_DIR}/scripts/remote.sh ${CLAUDE_SESSION_ID}`

Interpret only the status above. Report it to the user like this, and say nothing else.

If it says REMOTE_ENABLED:

    ● {{LABEL}} — connected to Telegram
      Local activity is mirrored there live.
      Run this command again to disconnect.

If it says REMOTE_ENABLED_WITH_WARNING: the same, plus a note that the initial
Telegram connection notification could not be delivered (the bridge may be down).

If it says REMOTE_DISABLED:

    ○ {{LABEL}} — disconnected
      Local activity stays in the terminal.

If it says ERROR, explain the error briefly.

Do not run any additional tools.
