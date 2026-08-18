# ---------------------------------------------------------------------------
# Example shell integration (zsh): one persistent tmux seat that both you and Telegram drive.
#
# Paste this block into ~/.zshrc. Nothing here is required by the package — it is how the
# reference machine keeps a single live Claude Code session for Agent2Telegram to attach to.
#
# The seat must be persistent: Telegram injects messages into it with `tmux send-keys`, so if
# the session dies, the remote side has nothing to talk to. The seat NAME is read from the
# bridge config, so the two can never drift apart.
#
# Set LCR_LAUNCHER to your CCR launcher (see examples/qwen-direct).
# ---------------------------------------------------------------------------

# >>> local claude remote seat >>>
LCR_LAUNCHER="${LCR_LAUNCHER:-$HOME/.local/bin/qwen-direct}"

# The single source of truth for the seat name: "tmux_session" in the bridge config.
_lcr_seat() {
    python3 - <<'PY'
import json
from pathlib import Path

p = Path.home() / ".config" / "agent2telegram" / "config.json"
try:
    seat = json.loads(p.read_text()).get("tmux_session", "")
except Exception:
    seat = ""
if not seat:
    raise SystemExit(1)
print(seat)
PY
}

_lcr_in_seat() {
    [[ -n "${TMUX:-}" ]] || return 1
    local current expected
    current="$(tmux display-message -p '#S' 2>/dev/null)" || return 1
    expected="$(_lcr_seat)" || return 1
    [[ "$current" == "$expected" ]]
}

_lcr_enter_seat() {
    local seat pane_cmd launch arg

    seat="$(_lcr_seat)" || {
        echo "Couldn't determine the Agent2Telegram tmux session."
        return 1
    }

    # The seat is a persistent shell; create it if it isn't there yet.
    if ! tmux has-session -t "$seat" 2>/dev/null; then
        tmux new-session -d -s "$seat" -c "$PWD" || return 1
        sleep 0.3
    fi

    pane_cmd="$(tmux display-message -p -t "$seat" '#{pane_current_command}' 2>/dev/null)"

    # Idle at a shell → start a session there. Already running → leave it alone and attach,
    # so re-running the function never stacks a second agent on a live conversation.
    case "$pane_cmd" in
        zsh|bash|fish|sh|dash)
            launch="cd -- ${(q)PWD} && ${(q)LCR_LAUNCHER}"
            for arg in "$@"; do
                launch+=" ${(q)arg}"
            done
            tmux send-keys -t "$seat" C-u
            tmux send-keys -t "$seat" -l -- "$launch"
            tmux send-keys -t "$seat" Enter
            sleep 0.25
            ;;
    esac

    if [[ -n "${TMUX:-}" ]]; then
        tmux switch-client -t "$seat"        # already in tmux → switch, don't nest
    else
        tmux attach-session -t "$seat"
    fi
}

qwen() {
    if _lcr_in_seat; then
        "$LCR_LAUNCHER" "$@"
    else
        _lcr_enter_seat "$@"
    fi
}

qwen-resume() {
    if _lcr_in_seat; then
        "$LCR_LAUNCHER" --resume "$@"
    else
        _lcr_enter_seat --resume "$@"
    fi
}
# <<< local claude remote seat <<<
