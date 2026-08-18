"""Installer for local Remote Control: the Skill, the hooks, and a health check.

Design rules, because this edits a file the user cares about:

  * **never** replace the ``hooks`` object wholesale — only this project's own entries are
    added or removed, everything else is left byte-for-byte alone;
  * back up ``settings.json`` (timestamped) before writing;
  * idempotent — running it twice changes nothing the second time;
  * report exactly what changed;
  * ``uninstall`` removes only what ``install`` added.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import core

#: Hook events the mirror listens to, with the timeout registered for each. ``MessageDisplay``
#: gets the tightest budget: it only writes one small file, so anything slower is a bug.
EVENT_TIMEOUTS = {
    "SessionStart": 5,
    "SessionEnd": 5,
    "UserPromptSubmit": 5,
    "MessageDisplay": 5,
    "PreToolUse": 5,                 # replaced at install time — AskUserQuestion waits
    "PostToolUse": 5,                # blocking dialogs only — see EVENT_MATCHERS
    "PostToolUseFailure": 5,
    "PermissionRequest": 5,          # replaced at install time — this one deliberately waits
    "Notification": 5,
    "SubagentStart": 5,
    "SubagentStop": 5,
    "TaskCreated": 5,
    "TaskCompleted": 5,
    "PreCompact": 5,
    "PostCompact": 5,
    "Elicitation": 5,
    "ElicitationResult": 5,
    "Stop": 5,
    "StopFailure": 5,
}

#: Events we scope with a matcher rather than taking every occurrence. ``PostToolUse`` fires for
#: every tool and carries ``tool_output``, which is large and the likeliest place for a secret —
#: we want it only to learn that a blocking question was answered.
EVENT_MATCHERS = {
    "PostToolUse": "|".join(core.BLOCKING_TOOLS),
}

#: Headroom on top of the remote-approval wait, so OUR timeout fires first and falls back to
#: the terminal prompt gracefully, rather than Claude Code killing the hook mid-wait.
PERMISSION_HOOK_HEADROOM = 30

#: Events that also need Agent2Telegram's own turn-end signal, so the attach bridge stops its
#: typing indicator and clears its status bubble at the exact end of a turn (including failures).
STOP_EVENTS = ("Stop", "StopFailure")

#: Substrings that identify a hook entry as belonging to this project (current or legacy), so
#: uninstall/re-install can find them without touching anybody else's hooks.
OWNED_MARKERS = (
    "remote_control/core.py",
    "remote_control\\core.py",
    "remote-control hook",
    "remote-control session-start",
    "agent2telegram.stop_hook",
    "qwen-telegram-stop-hook",          # legacy wrapper this project replaces
    "qwen-remote-control session-start",
)


# --------------------------------------------------------------------------- helpers

def claude_config_dir(explicit: str = "") -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser()
    return Path.home() / ".claude"


def hook_python(explicit: str = "") -> str:
    return explicit or sys.executable or "python3"


def hook_argv(python: str) -> list:
    """The registered fast hook command, as argv.

    ``-S -E`` skips ``site`` processing and ``PYTHONPATH``, which roughly halves interpreter
    start-up. It is safe here precisely because :mod:`agent2telegram.remote_control.core` is
    standard-library only and has no relative imports, so it runs fine as a plain script.
    """
    return [python, "-S", "-E", str(Path(core.__file__).resolve())]


def hook_command(python: str) -> str:
    """The same command as one properly quoted string, which is what settings.json takes."""
    return shlex.join(hook_argv(python))


def stop_command(python: str) -> str:
    return f"{shlex.quote(python)} -m agent2telegram.stop_hook"


def _is_ours(entry: dict) -> bool:
    blob = json.dumps(entry, ensure_ascii=False)
    return any(m in blob for m in OWNED_MARKERS)


def _strip_ours(groups: list) -> tuple[list, int]:
    """Drop this project's hook entries from *groups*, keeping everyone else's intact."""
    removed = 0
    out = []
    for group in groups:
        if not isinstance(group, dict):
            out.append(group)
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            out.append(group)
            continue
        kept = [h for h in hooks if not (isinstance(h, dict) and _is_ours(h))]
        removed += len(hooks) - len(kept)
        if not kept:
            continue                       # the whole group was ours → drop it
        if len(kept) != len(hooks):
            group = {**group, "hooks": kept}
        out.append(group)
    return out, removed


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.bak-remote-control-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


def _read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


# --------------------------------------------------------------------------- skill

def _skill_source() -> Path:
    return Path(__file__).resolve().parent / "skill"


def render_skill(dest: Path, *, skill_name: str, label: str, python: str,
                 config: str = "", permissions: bool = True,
                 permission_timeout: float = core.PERMISSION_TIMEOUT,
                 question_timeout: float = core.QUESTION_TIMEOUT) -> list[str]:
    """Materialize the Skill with this machine's paths. Returns the files written."""
    src = _skill_source()
    config_arg = f" \\\n    --config {shlex.quote(config)}" if config else ""
    perm_args = (f" \\\n    --permission-timeout {permission_timeout:g}"
                 f" \\\n    --question-timeout {question_timeout:g}")
    if not permissions:
        perm_args += " \\\n    --no-permission-prompts"
    subs = {
        "{{SKILL_NAME}}": skill_name,
        "{{LABEL}}": label,
        "{{LABEL_SHELL}}": shlex.quote(label),
        "{{PYTHON}}": shlex.quote(python),
        "{{CONFIG_ARG}}": config_arg,
        "{{PERMISSION_ARGS}}": perm_args,
    }
    written = []
    for rel in ("SKILL.md", "scripts/remote.sh"):
        text = (src / rel).read_text("utf-8")
        for k, v in subs.items():
            text = text.replace(k, v)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        os.chmod(target, 0o700 if rel.endswith(".sh") else 0o600)
        written.append(str(target))
    return written


# --------------------------------------------------------------------------- install

def install(args) -> int:
    changes: list[str] = []
    problems: list[str] = []

    cdir = claude_config_dir(getattr(args, "claude_config_dir", ""))
    if not cdir.is_dir():
        print(f"✗ Claude config dir not found: {cdir}", file=sys.stderr)
        return 2
    if shutil.which("claude") is None:
        problems.append("the 'claude' binary is not on PATH (Claude Code may be installed "
                        "elsewhere; hooks still work, but check your launcher)")

    from .cli import find_config
    cfg_path, cfg = find_config(getattr(args, "agent2telegram_config", ""),
                                getattr(args, "tmux_session", ""))
    if not cfg_path:
        print("✗ no Agent2Telegram bridge config found — run 'agent2telegram setup' first, "
              "or pass --agent2telegram-config / --tmux-session", file=sys.stderr)
        return 2
    if cfg.get("agent") != "claude-code":
        problems.append(f"bridge {cfg_path.name} drives '{cfg.get('agent')}', not claude-code")
    if cfg.get("mode") == "attach":
        if shutil.which("tmux") is None:
            print("✗ attach mode needs tmux, which is not installed", file=sys.stderr)
            return 2
        if not cfg.get("tmux_session"):
            print("✗ attach mode needs 'tmux_session' in the bridge config", file=sys.stderr)
            return 2

    python = hook_python(getattr(args, "python", ""))
    rc_cmd = hook_command(python)
    stop_cmd = stop_command(python)
    skill_name = getattr(args, "skill_name", "local-remote")
    label = getattr(args, "label", "Remote Control")
    skill_dir = cdir / "skills" / skill_name
    dry = getattr(args, "dry_run", False)

    # ---- Skill
    permissions = not getattr(args, "no_permission_prompts", False)
    permission_timeout = float(getattr(args, "permission_timeout", core.PERMISSION_TIMEOUT))
    question_timeout = float(getattr(args, "question_timeout", core.QUESTION_TIMEOUT))
    if dry:
        changes.append(f"would install Skill → {skill_dir}")
    else:
        written = render_skill(skill_dir, skill_name=skill_name, label=label,
                               python=python, config=str(cfg_path),
                               permissions=permissions, permission_timeout=permission_timeout,
                               question_timeout=question_timeout)
        changes.append(f"installed Skill → {skill_dir} ({len(written)} files)")
    changes.append("remote decisions: "
                   + (f"on — permissions wait {permission_timeout:g}s, questions "
                      f"{question_timeout:g}s, then the terminal takes over"
                      if permissions else "off (notification only)"))

    # ---- settings.json
    settings_path = cdir / "settings.json"
    try:
        settings = _read_settings(settings_path)
    except (OSError, ValueError) as e:
        print(f"✗ cannot read {settings_path}: {e}", file=sys.stderr)
        return 2

    before = json.dumps(settings, sort_keys=True)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print(f"✗ {settings_path}: 'hooks' is not an object", file=sys.stderr)
        return 2

    timeouts = dict(EVENT_TIMEOUTS)
    # The approval hook blocks on purpose while a human decides, so it needs a real budget.
    timeouts["PermissionRequest"] = int(permission_timeout) + PERMISSION_HOOK_HEADROOM
    # PreToolUse blocks only for AskUserQuestion; for every other tool it still returns in ms.
    timeouts["PreToolUse"] = int(question_timeout) + PERMISSION_HOOK_HEADROOM
    removed_total = 0
    for event, timeout in timeouts.items():
        groups = hooks.get(event) or []
        if not isinstance(groups, list):
            problems.append(f"hooks.{event} is not a list — left untouched")
            continue
        groups, removed = _strip_ours(groups)
        removed_total += removed
        entries = [{"type": "command", "command": rc_cmd, "timeout": timeout}]
        if event in STOP_EVENTS:
            entries.insert(0, {"type": "command", "command": stop_cmd, "timeout": 15})
        group = {"hooks": entries}
        if event in EVENT_MATCHERS:
            group = {"matcher": EVENT_MATCHERS[event], **group}
        groups.append(group)
        hooks[event] = groups

    # Let the Skill's helper run without a permission prompt (additive, deduped).
    perms = settings.setdefault("permissions", {})
    if isinstance(perms, dict):
        allow = perms.setdefault("allow", [])
        if isinstance(allow, list):
            script = skill_dir / "scripts" / "remote.sh"
            for rule in (f"Bash({script})", f"Bash({script} *)"):
                if rule not in allow:
                    allow.append(rule)
                    changes.append(f"allowed {rule}")

    after = json.dumps(settings, sort_keys=True)
    if before == after:
        changes.append(f"{settings_path}: already up to date")
    elif dry:
        changes.append(f"would update {settings_path} "
                       f"({len(timeouts)} events, {removed_total} stale entries removed)")
    else:
        backup = _backup(settings_path)
        _write_settings(settings_path, settings)
        changes.append(f"backed up {settings_path} → {backup}" if backup else "created "
                       f"{settings_path}")
        changes.append(f"registered {len(timeouts)} hook events "
                       f"({removed_total} stale/legacy entries removed)")

    print(("Dry run — nothing was changed.\n" if dry else "") + "\n".join("  • " + c
                                                                         for c in changes))
    if problems:
        print("\nWarnings:")
        for p in problems:
            print("  ! " + p)
    print(f"\nRestart the Claude Code session, then run /{skill_name} to connect.")
    return 0


# --------------------------------------------------------------------------- uninstall

def uninstall(args) -> int:
    cdir = claude_config_dir(getattr(args, "claude_config_dir", ""))
    skill_name = getattr(args, "skill_name", "local-remote")
    dry = getattr(args, "dry_run", False)
    changes = []

    settings_path = cdir / "settings.json"
    try:
        settings = _read_settings(settings_path)
    except (OSError, ValueError) as e:
        print(f"✗ cannot read {settings_path}: {e}", file=sys.stderr)
        return 2
    hooks = settings.get("hooks")
    removed = 0
    if isinstance(hooks, dict):
        for event in list(hooks):
            groups = hooks.get(event)
            if not isinstance(groups, list):
                continue
            groups, n = _strip_ours(groups)
            removed += n
            if groups:
                hooks[event] = groups
            else:
                hooks.pop(event)
    if removed and not dry:
        backup = _backup(settings_path)
        _write_settings(settings_path, settings)
        changes.append(f"backed up {settings_path} → {backup}")
    changes.append(f"{'would remove' if dry else 'removed'} {removed} hook entries")

    skill_dir = cdir / "skills" / skill_name
    if skill_dir.is_dir():
        if not dry:
            shutil.rmtree(skill_dir, ignore_errors=True)
        changes.append(f"{'would remove' if dry else 'removed'} Skill {skill_dir}")

    if not getattr(args, "keep_state", False):
        root = Path(core.root_dir())
        if root.is_dir():
            if not dry:
                shutil.rmtree(root, ignore_errors=True)
            changes.append(f"{'would remove' if dry else 'removed'} state {root}")

    print("\n".join("  • " + c for c in changes))
    print("\nUnrelated Claude Code hooks and the Agent2Telegram config were left untouched.")
    return 0


# --------------------------------------------------------------------------- doctor

def doctor(args) -> int:
    ok = True
    cdir = claude_config_dir(getattr(args, "claude_config_dir", ""))
    print(f"claude config dir : {cdir}{'' if cdir.is_dir() else '   ✗ missing'}")
    ok &= cdir.is_dir()

    settings_path = cdir / "settings.json"
    registered = set()
    try:
        settings = _read_settings(settings_path)
        for event, groups in (settings.get("hooks") or {}).items():
            if isinstance(groups, list) and any(
                    isinstance(g, dict) and any(
                        isinstance(h, dict) and "remote_control" in json.dumps(h)
                        for h in (g.get("hooks") or []))
                    for g in groups):
                registered.add(event)
    except (OSError, ValueError) as e:
        print(f"settings.json     : ✗ {e}")
        ok = False
    missing = sorted(set(EVENT_TIMEOUTS) - registered)
    print(f"hook events       : {len(registered)}/{len(EVENT_TIMEOUTS)} registered"
          + (f"   ✗ missing: {', '.join(missing)}" if missing else "   ✓"))
    ok &= not missing

    skill = cdir / "skills" / getattr(args, "skill_name", "local-remote") / "SKILL.md"
    print(f"skill             : {skill}{'   ✓' if skill.exists() else '   ✗ missing'}")
    ok &= skill.exists()

    from .cli import find_config
    cfg_path, cfg = find_config(getattr(args, "config", ""), getattr(args, "tmux_session", ""))
    if cfg_path:
        bridge = cfg.get("tmux_session") or cfg_path.stem
        alive = core.consumer_alive(bridge)
        print(f"bridge config     : {cfg_path}   ✓")
        print(f"bridge consumer   : {'live' if alive else 'not consuming (bridge stopped?)'}"
              f"   {'✓' if alive else '✗'}")
        print(f"spool             : {core.pending_count(bridge)} pending event(s)")
        ok &= alive
    else:
        print("bridge config     : ✗ none matches this tmux session")
        ok = False

    if shutil.which("tmux") is None:
        print("tmux              : ✗ not installed (attach mode needs it)")
        ok = False
    else:
        print("tmux              : ✓")
    print(f"claude binary     : {shutil.which('claude') or '✗ not on PATH'}")

    argv = hook_argv(hook_python())          # argv list, never a shell string
    probe = subprocess.run(argv, input="{}", text=True, capture_output=True, timeout=30)
    print(f"hook command      : {'✓' if probe.returncode == 0 else '✗'} {shlex.join(argv)}")
    ok &= probe.returncode == 0

    print("\n" + ("✓ Remote Control looks healthy." if ok else "✗ Some checks failed."))
    return 0 if ok else 1
