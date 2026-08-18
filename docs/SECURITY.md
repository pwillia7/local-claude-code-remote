# Security

This project mirrors a coding agent that can run arbitrary code on your machine. Treat the
Telegram chat as a control channel for that machine.

Upstream Agent2Telegram's policy still applies — see [`../SECURITY.md`](../SECURITY.md). This
document covers what the Remote Control layer adds.

---

## Trust model

* **Only allow-listed Telegram user ids** can send anything into the session. Everyone else is
  refused. The Remote Control layer does not touch that allow-list and does not widen it.
* **Outbound mirroring goes to one chat**: the first entry in `allowed_user_ids`, the same
  destination upstream already uses.
* **Mirroring is off by default and per session.** It only starts when *you* invoke the Skill in
  that session, and the Skill is marked `disable-model-invocation: true`, so the model cannot
  turn it on. A fresh `startup`, `resume` or `fork` resets it to off.
* **No inbound network surface.** No listening port, no webhook, no HTTP server. Telegram long
  polling only.
* **No automatic permission decisions.** `PermissionRequest` is surfaced as a notification and
  nothing more. No `--dangerously-skip-permissions` is added, no permission mode is changed, and
  no tmux keystroke ever simulates an approval. Approving remotely is
  [future work](../README.md#future-work), not a hidden feature.

## What the mirror can put in a chat

Assistant text, local prompts, tool summaries, subagent/task names and error messages — i.e.
**anything the model prints**. That can legitimately include source code, file paths, log output
and, if the model was handed one, a credential.

Mitigations:

* Tool summaries read only known-safe fields (`description`, `command`, `file_path`, `pattern`,
  `url`, `query`) and are then **redacted** for common credential shapes — `FOO_TOKEN=…`,
  `Bearer …`, `sk-…`, `ghp_…`, `xox…`, AWS key ids, JWTs, Telegram bot tokens — and truncated to
  one short line.
* `PostToolUse` is deliberately **not** registered: `tool_output` is large and is the most likely
  place for a secret to appear.
* Full hook payloads are never logged. Diagnostics log event *types* and counts, not contents.

Redaction is a safety net for accidents, not a guarantee. Anyone with access to the Telegram
account can read whatever the session displays — protect that account.

## What is stored on disk, and for how long

Under `$AGENT2TELEGRAM_STATE/remote-control/` (default `~/.local/state/agent2telegram/`):

| Path | Contents | Mode | Lifetime |
| --- | --- | --- | --- |
| `sessions/<id>.json` | bridge name, config path, origin prefixes | `0600` | until disconnect |
| `<bridge>/enabled/<id>` | marker only | `0600` | until disconnect |
| `<bridge>/origin/<id>.json` | `terminal` or `telegram` | `0600` | until disconnect |
| `<bridge>/events/*.json` | **message text, prompts, tool summaries** | `0600` | deleted the moment it is forwarded |
| `<bridge>/consumer_heartbeat` | a unix timestamp | `0600` | rewritten each cycle |

Directories are `0700`. The spool is the only place content lives, and it is transient by
design: an event file is deleted as soon as it has been applied or handed to the bridge's own
durable retry queue. If the bridge is stopped, events wait there — so a machine other people can
read is a machine where you should stop the mirror.

`remote-control uninstall` removes the whole tree (`--keep-state` opts out).

## Secrets

The Telegram bot token and the optional ElevenLabs key live **only** in
`~/.config/agent2telegram/config.json` (`0600`, in a `0700` directory), which is
`.gitignore`d and must never be committed. The Remote Control layer:

* reads the token only inside the toggle's own process, to send the connect/disconnect notice;
* never passes it on a command line, into an environment variable, or into a log;
* never writes it into the spool, the Skill, or `settings.json`.

The installed Skill contains no credentials — only paths.

## Things this project will not do

* open an inbound port, a webhook or a tunnel;
* auto-approve tool permissions or weaken the permission mode;
* scrape the terminal for content;
* run a second Telegram poller against the same bot token (Telegram allows one; running two
  makes updates disappear at random);
* retain message payloads after forwarding them.

## Reporting

Report vulnerabilities the way upstream asks in [`../SECURITY.md`](../SECURITY.md). For issues
specific to the Remote Control layer, open a private report on this fork's repository rather
than a public issue.
