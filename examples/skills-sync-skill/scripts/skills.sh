#!/usr/bin/env bash
# Thin wrapper so the Skill can call the tool through one allow-listed path.
set -euo pipefail

# CLAUDE_CONFIG_DIR is set for this session by the harness, and the tool uses it to find the
# profile it should be comparing against — so this works without any hard-coded paths.
for candidate in \
    "${CLAUDE_PROFILE_SKILLS_BIN:-}" \
    "$HOME/.local/bin/claude-profile-skills" \
    "$(command -v claude-profile-skills 2>/dev/null || true)"
do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
        exec "$candidate" "$@"
    fi
done

echo "ERROR: claude-profile-skills is not installed or not executable."
echo "Install it from examples/claude-profile-skills in the local-claude-code-remote repo."
exit 1
