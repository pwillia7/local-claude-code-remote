"""Local Claude Code Remote Control — a fork of Agent2Telegram (MIT, petrludwig-collab).

Upstream connects a coding agent (Claude Code or Codex) to Telegram. This fork adds
:mod:`agent2telegram.remote_control`: a hook-driven mirror of a **local, interactive** Claude
Code session, for harnesses where native Claude Code Remote Control cannot work because the
model backend is an LLM gateway (Claude Code Router, a proxy, …) rather than api.anthropic.com.
Everything upstream does still works unchanged.

A small, dependency-free bridge: it long-polls Telegram for messages from authorized
users, hands each message to the configured agent CLI, and streams the reply back.

Design goals (in priority order):
  1. Robustness — degrade gracefully, never crash the main loop on a single bad message.
  2. Security — only allow-listed Telegram users can drive the agent (it runs code!).
  3. Zero install friction — the core uses only the Python standard library.
"""

__version__ = "1.2.0+local-remote.1"
