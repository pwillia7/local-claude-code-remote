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
* **No automatic permission decisions.** A permission request is decided by a *person* pressing
  a button, or by nobody. Nothing is approved on a timer, on a heuristic, or by the model. No
  `--dangerously-skip-permissions` is added, no permission mode is changed, and no tmux
  keystroke ever simulates an approval.

## Remote permission approval

This is the part of the design that changes what the Telegram side can *do*, so it deserves a
clear statement.

**What it grants.** An allow-listed Telegram user can press ✅ Allow and let a tool call run.

**Why that is not an escalation.** That user can already send arbitrary prompts into a live
Claude Code session, which is a strictly larger capability: they could simply ask for the same
thing. The approval button gives a *faster* path to something they already control, not a new
one. If you would not trust someone to press Allow, they must not be in `allowed_user_ids` at
all — see the trust model above.

**The constraints that hold regardless:**

* `callback_query.from.id` is checked against `allowed_user_ids` on every press. An unknown
  presser gets "Not authorized", the buttons stay live, and nothing is recorded.
* A press is honoured **once**. The request is removed from the pending map under a lock before
  a decision is written, so a replayed or double press decides nothing.
* Buttons are retracted when the request is answered, when it expires, and at turn end — a card
  never outlives the decision it was for.
* An Allow press cannot loosen your settings: a hook `allow` does not override `deny` rules, and
  it cannot suppress prompts your organization forces to `ask`.
* No answer means **no decision** — the hook prints nothing, and Claude Code's own prompt appears
  at the terminal. Timeouts never imply consent.
* The bot token still never leaves the bridge process; the decision travels as a `0600` file on
  the local filesystem, not over the network.
* Don't want it? `--no-permission-prompts` reverts to a notification, and `remote-control off`
  ends it for that session.

**What the card shows.** The tool name, the one-line summary, and one redacted detail line
(the Bash command, the file path, the URL, …). For MCP tools it names the argument *keys* only,
never their values, because MCP arguments are server-defined and arbitrary. It is enough to
decide on; it is not a dump of `tool_input`.

**Residual risk.** Anyone who controls the Telegram account can approve tool calls on this
machine. That was already true of anyone who could send it prompts — protect the account, keep
the allow-list to yourself, and turn mirroring off when you are done with a session.

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
| `<bridge>/decisions/*.json` | an Allow/Deny and who pressed it | `0600` | deleted by the hook that collects it; swept after 1 h |
| `<bridge>/consumer_heartbeat` | a unix timestamp | `0600` | rewritten each cycle |
| `<bridge>/start.lock` | a pid | `0600` | held only while a bridge is starting |

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
  makes updates disappear at random) — the bridge auto-start is explicitly built to refuse this;
* approve a tool call without a human pressing a button;
* retain message payloads after forwarding them.

## Reporting

Report vulnerabilities the way upstream asks in [`../SECURITY.md`](../SECURITY.md). For issues
specific to the Remote Control layer, open a private report on this fork's repository rather
than a public issue.
