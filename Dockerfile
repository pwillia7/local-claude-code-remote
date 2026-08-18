# Local Claude Code Remote Control — container image (forked from Agent2Telegram's).
#
# This builds THIS repository (COPY . /app), so the image includes Remote Control. Note that the
# hook side runs wherever Claude Code runs, not in here: a containerized bridge can serve a
# Claude Code session on the host only if they share the state directory and the tmux socket.
#
# The bridge core has no Python dependencies, so this image is tiny. NOTE: the agent CLI
# you connect (Claude Code / Codex) and its login are NOT baked in — mount
# them or install in a derived image, because each requires interactive authentication
# that must not live in a public image. See docs/UPSTREAM_README.md ("Docker") for the setup.
FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .

# Config and per-chat state live on a mounted volume so they survive restarts.
ENV AGENT2TELEGRAM_CONFIG=/data/config.json \
    AGENT2TELEGRAM_STATE=/data/state
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "agent2telegram"]
CMD ["run"]
