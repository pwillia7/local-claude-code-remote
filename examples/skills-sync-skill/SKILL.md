---
name: skills-sync
description: List, link or ignore Claude Code skills that are installed personally (~/.claude/skills) but invisible to this harness, because it runs with its own CLAUDE_CONFIG_DIR. Use when a globally installed skill never shows up here.
argument-hint: "[link <name>… | link --all | ignore <name>… | unignore <name>… | unlink <name>…]"
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/skills.sh *)
---

!`${CLAUDE_SKILL_DIR}/scripts/skills.sh $ARGUMENTS`

Report the output above to the user, briefly and as-is. It is the authoritative result.

If skills were linked, remind them the harness picks up new skills on the **next** session, not
this one.

If skills are listed as available but not linked, offer the two choices and nothing more:

    /skills-sync link <name>      make it visible to this harness
    /skills-sync ignore <name>    stop mentioning it

Do not run any other tools, and do not link or ignore anything the user did not ask for —
each linked skill costs context in every future session, so the choice is theirs.
