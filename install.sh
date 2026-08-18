#!/usr/bin/env bash
# Local Claude Code Remote Control — installer (forked from Agent2Telegram's).
#
# Usage:  ./install.sh                       run it from inside a clone of THIS repo
#         A2T_REPO=<url> ./install.sh        install from a specific clone URL
#
# It checks Python, installs the package for the current user, and launches the Agent2Telegram
# setup wizard. That gets you the bridge; Remote Control itself needs one more step, which this
# script prints at the end:
#
#     python3 -m agent2telegram remote-control install --claude-config-dir ...
#
# NOTE: this fork does not have a public clone URL baked in on purpose. Upstream's installer
# hard-coded petrludwig-collab/Agent2Telegram, which would quietly install a package WITHOUT
# Remote Control — the one failure this script must not have. Run it from a clone, or set
# A2T_REPO yourself.
set -euo pipefail

# Recover the working directory if it was deleted (e.g. you just uninstalled while sitting in
# the source clone) — otherwise git/curl fail with "cannot access parent directories: getcwd".
cd "$PWD" 2>/dev/null || cd "$HOME" 2>/dev/null || cd /

# Deliberately empty: see the note above. Set A2T_REPO to install from a clone URL.
REPO="${A2T_REPO:-}"
NEED_PY_MAJOR=3
NEED_PY_MINOR=10

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# Pick where to drop the launcher. Prefer a directory ALREADY on PATH and writable — then the
# command works in the *current* terminal immediately (a piped installer can't change the parent
# shell's PATH, so installing into a dir it already searches is the only way to "just work" now).
# Fall back to ~/.local/bin (added to PATH via the rc files for future shells).
pick_bindir() {
  oldifs="$IFS"; IFS=:
  for d in $PATH; do
    case "$d" in
      "$HOME"/*) if [ -d "$d" ] && [ -w "$d" ]; then IFS="$oldifs"; printf '%s' "$d"; return; fi ;;
    esac
  done
  IFS="$oldifs"; printf '%s' "$HOME/.local/bin"
}

# 1) Python check
PY="$(command -v python3 || true)"
[ -n "$PY" ] || err "python3 not found. Install Python ${NEED_PY_MAJOR}.${NEED_PY_MINOR}+ first."
"$PY" - <<'PYEOF' || err "Python ${NEED_PY_MAJOR}.${NEED_PY_MINOR}+ required."
import sys
sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)
PYEOF
say "Using $("$PY" --version)"

# 2) Get the code (clone if we're not already inside it)
if [ -f "pyproject.toml" ] && grep -q "agent2telegram" pyproject.toml 2>/dev/null; then
  SRC="$(pwd)"
  say "Installing from current directory"
else
  [ -n "$REPO" ] || err "Run this from inside a clone of this repository, or set A2T_REPO to its
       clone URL. (Refusing to guess: installing the wrong repository would give you a bridge
       with no Remote Control, and you would not find out until nothing mirrored.)"
  command -v git >/dev/null || err "git not found (needed to fetch the project)."
  SRC="${HOME}/.local-claude-code-remote-src"
  if [ -d "$SRC/.git" ]; then say "Updating $SRC"; git -C "$SRC" pull --ff-only
  else say "Cloning $REPO into $SRC"; git clone --depth 1 "$REPO" "$SRC"; fi
fi

# 3) Make `agent2telegram` a real command. pip is OPTIONAL (the core is pure standard library):
#    if pip is there we install the package too (so hooks can `python3 -m agent2telegram…`), but
#    EITHER WAY we drop our own launcher into a directory on PATH so the command works right after
#    install — no PYTHONPATH to remember, and (when a PATH dir is writable) no new shell needed.
if "$PY" -m pip --version >/dev/null 2>&1 || "$PY" -m ensurepip --upgrade >/dev/null 2>&1; then
  say "Installing the package"
  "$PY" -m pip install --user --upgrade "$SRC" >/dev/null 2>&1 \
    || "$PY" -m pip install --user --break-system-packages --upgrade "$SRC" >/dev/null 2>&1 || true
fi

BIND="$(pick_bindir)"
mkdir -p "$BIND"
printf '#!/bin/sh\nexec env PYTHONPATH="%s" "%s" -m agent2telegram "$@"\n' "$SRC" "$PY" > "$BIND/agent2telegram"
chmod +x "$BIND/agent2telegram"
RUN=("$BIND/agent2telegram"); HOW="agent2telegram"
case ":$PATH:" in
  *":$BIND:"*)
    say "Installed launcher in $BIND (already on PATH) — 'agent2telegram' works now." ;;
  *)
    say "Installed launcher in $BIND — adding it to PATH for new shells."
    for rc in "$HOME/.bashrc" "$HOME/.profile" "$HOME/.zshrc"; do
      [ -e "$rc" ] || continue
      grep -qs "$BIND" "$rc" || echo "export PATH=\"$BIND:\$PATH\"" >> "$rc"
    done
    # ~/.bashrc may not exist on a minimal server — create it so login shells pick it up.
    grep -qs "$BIND" "$HOME/.bashrc" 2>/dev/null || echo "export PATH=\"$BIND:\$PATH\"" >> "$HOME/.bashrc"
    export PATH="$BIND:$PATH"
    say "For THIS terminal, run:  export PATH=\"$BIND:\$PATH\"   (new terminals get it automatically)" ;;
esac

# 4) Launch the setup wizard, then point at the Remote Control step.
# When invoked as `curl … | bash`, this script's stdin is the pipe, not your keyboard,
# so the interactive wizard must read from the controlling terminal (/dev/tty).
say "Run the bridge later with:  $HOW run"
cat <<'NEXT'

==> After the wizard, install Remote Control itself:

      python3 -m agent2telegram remote-control install           --claude-config-dir "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"           --tmux-session <your-tmux-session>           --dry-run

    Drop --dry-run once the reported changes look right, then check it with:

      python3 -m agent2telegram remote-control doctor

    Or let Claude Code do all of it: copy skills/local-remote-setup into your Claude
    config dir's skills/ and ask it to set this up. See skills/README.md.

NEXT
if [ -e /dev/tty ]; then
  say "Starting the bridge setup wizard…"
  exec "${RUN[@]}" setup </dev/tty
else
  say "Installed. Finish the bridge setup with:"
  echo "    $HOW setup"
fi
