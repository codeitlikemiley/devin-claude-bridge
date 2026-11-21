# Devin ↔ Claude Code bridge

A local **MCP stdio server** that lets one Claude Code act as chief of staff for
**multiple Devin accounts/organizations and their sessions (Bots)**. Python 3.10+;
no Node, webhook server, inbound port, or public deployment required.

* **Claude → Devin:** create a session or send a message through the Devin v3 API.
* **Devin → Claude:** Devin replies in its own session (its `message_user` tool).
  Claude polls those replies with `read_messages`, `wait_for_reply`, or `check_inbox`.
  Devins never connect to Claude's machine. This is polling, not push: Claude must
  call a tool, or a hook/cron job must invoke the inbox CLI.

## Setup

1. For each Devin account, create a **Personal Access Token** at
   **app.devin.ai → Settings → Devin API → PATs** (recommended for this local bridge).
   PATs work with any plan's organization, including individual Pro/Max accounts,
   and can call `/v3/organizations/{org_id}/*` for every org your user can access.
   They inherit your user permissions; enterprise PAT policies may require approval.
   Find the `org-...` organization ID at the top of **Settings → Devin API**.
   Service-user API keys are an alternative, especially for shared automation:
   grant session creation, reading and messaging permissions in the intended org.
   `ViewOrgSessions` is required for listing org sessions and discovering Bots.
2. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and export
   keys in the environment that launches Claude Code. For a single account, no
   config file is needed:

   ```sh
   export DEVIN_API_KEY='your-key'
   export DEVIN_ORG_ID='org-your-org'
   ```

   This account is named `default`. For multiple accounts, copy
   [`config.example.toml`](config.example.toml) to
   `~/.config/devin-claude-bridge/config.toml`, replace org/session IDs, and export
   `DEVIN_MAX2_API_KEY` and `DEVIN_WORK_API_KEY`. `[accounts.<name>]` supports
   `org_id`, `api_key_env` (preferred) or `api_key`, and optional `base_url`.
   If `api_key_env` is set, it takes precedence and must be present/nonempty.
   `[targets.<alias>]` supports `account`, exactly one of `session_id` or
   `bot_service_user_id`, and optional `description`.
   Never commit real keys; restrict config permissions with `chmod 600` if it
   contains literal keys. `base_url` receives the bearer key: only use trusted URLs.
3. Register the server:

   ```sh
   claude mcp add devin-bridge -- uvx --from git+https://github.com/codeitlikemiley/devin-claude-bridge devin-claude-bridge
   ```

   Restart Claude Code if necessary. The default command is `serve` (stdio).
   You can also install with `pip install .` and run `devin-claude-bridge serve`.

Config override: `DEVIN_BRIDGE_CONFIG=/absolute/path/config.toml`. An explicit
missing file is an error. Environment single-account fallback applies only when
there are no configured accounts; it does not silently merge into named accounts.

## Talking to your Devins

Example prompts for Claude:

* “Ask research-bot on account max2 to investigate the flaky integration tests.”
* “Create a session on work to review this design, then watch it.”
* “Check my Devin inbox.”
* “Send the follow-up to research-bot, then wait for a new reply.”

Tell the Devin: **“Reply here in this session with your findings and any questions.
Claude will poll this session; you don't need an MCP connection to her machine.”**
The alias already identifies its account; tool calls use either `target` OR
`account` plus `session_id`, never both. Session IDs accept or omit `devin-`.

| Tool | Behavior |
| --- | --- |
| `list_accounts()` | Names, org IDs, and targets; never keys |
| `list_bots(account)` | Root conversations grouped by service user; newest session ID, title and status per Bot |
| `list_sessions(account, limit=100)` | Returns API page (`items`, `end_cursor`, `has_next_page`); limit 1–200 |
| `create_session(account, prompt, title?)` | Creates and watches; returns the API session object |
| `send_message(message, target? / account+session_id)` | Sends and watches; returns the updated API session object |
| `read_messages(target? / account+session_id, only_new=true, include_user=false)` | Reads every page, returns messages, and advances the shared last-seen position |
| `wait_for_reply(target? / account+session_id, timeout_seconds=300, poll_seconds=15)` | Marks existing messages seen, then waits for the first batch of new Devin replies; returns `timed_out` |
| `check_inbox(account?)` | New Devin messages across all watched sessions, grouped by account/session |
| `watch_session(target? / account+session_id)` | Starts watching without changing an existing cursor |
| `unwatch_session(target? / account+session_id)` | Stops watching and forgets the cursor |

Targets are shortcuts, not automatically watched. Watch them explicitly or send
them a message. First reads/watches include existing history; `only_new=false`
replays all history. User messages are excluded by default but still advance the
cursor. `wait_for_reply` deliberately discards previously unread history, including
replies that arrived before the call: use `read_messages` after sending if you want
to catch those too. Timeout is 0–3600 seconds; poll interval is >0–300 seconds.
The initial history fetch and state-lock acquisition may add API latency to a wait.
There is a final poll at the deadline, even when the timeout is shorter than the
poll interval; that request can add network latency to the timeout.

### Bot targets that survive recycling

Bots get a new conversation session when recycled or restarted. Prefer their
stable `service-user-...` ID over pinning their current `devin-...` session:

```toml
[targets.research-bot]
account = "max2"
bot_service_user_id = "service-user-your-bot"
description = "Research Bot"
```

Ask Claude to call `list_bots("max2")` to find this ID. The tool groups non-archived
root sessions by `service_user_id`; ordinary service users can appear too, so
identify your Bot by its conversation title. Child sessions are excluded.
The latest session is chosen by `created_at`. Bot target resolution caches the
newest non-archived, non-terminal root session in state, using the API's
`service_user_ids` filter. Query model fields are sent as ordinary URL query
parameters (`service_user_ids=service-user-...&is_archived=false`), not a JSON `qs`
string; arrays use repeated keys (OpenAPI form/explode encoding).

On each target call, the cached session's status is checked. `exit` and `error`
are terminal; `suspended` is not (sending can resume it). Terminal or archived
sessions, and 404/410 responses, trigger fresh resolution. A failed operation is
retried at most once on the newly resolved session. Inbox watches retain the alias
while moving to the new session, with a separate cursor for that session, so an
old conversation cursor is never applied to a new one. Unread replies from the
previous conversation are drained too (and carry their original `session_id`),
including when a send has already moved the alias. A terminal conversation with
no successor remains readable; sending still requires an active root. If the old
messages endpoint itself returns 404/410, its replies cannot be recovered.
The cache records the Bot service user ID, so changing an alias to a different
Bot invalidates it rather than sending to the old Bot. Unwatching a Bot
forgets its alias/cache without making network requests. Explicit `session_id`
targets keep their original behavior and never switch sessions automatically.
Removed/reconfigured aliases are reported in `skipped_targets` by inbox without
failing the CLI. Remove their saved watches with `unwatch_session(target="old-alias")`
even after deleting the config entry, or unwatch any entry by account/session ID
(which removes all aliases watching that specific entry).

State lives at `~/.local/state/devin-claude-bridge/state.json`, overridden by
`DEVIN_BRIDGE_STATE`. It stores watched sessions and last-seen cursor/event ID,
not API keys or message bodies. Writes are atomic and serialized with a POSIX
file lock (Linux/macOS). MCP and CLI share consumption: inbox messages read by
one are no longer new to the other. Avoid concurrent inbox polling and waits on
the same session if each consumer needs to see every reply. Invalid/corrupt state
is reported rather than silently reset.

### Hooks / cron

```sh
uvx --from git+https://github.com/codeitlikemiley/devin-claude-bridge devin-claude-bridge inbox
# or, once installed:
devin-claude-bridge inbox --account max2
```

Outputs JSON (`{"sessions": []}` when empty), suitable for a Claude Code hook or
cron. Arrange for your hook to surface the output to Claude; cron alone doesn't
inject messages into her conversation. Ensure that runner has the same config,
state path, and key environment. Individual session errors appear alongside
successful groups, retain the failed session's cursor, and make CLI exit nonzero.
401/403/404/409/410 errors include a readable explanation and API `detail`;
sending may auto-resume a suspended session, but blocked/terminated sessions can
still return errors. This bridge does not automatically retry sends or creations
because those operations are not idempotent.

## Development

```sh
pip install -e '.[dev]'
pytest -q
ruff check .
```

Tests use HTTPX MockTransport, no live keys or API access required. CI runs Python
3.10 and 3.13. The official `mcp` SDK provides FastMCP and stdio transport.
API shapes are based on the [v3 session docs](https://docs.devin.ai/api-reference/v3/sessions),
including [create](https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions),
[messages](https://docs.devin.ai/api-reference/v3/sessions/get-organizations-session-messages),
and [send](https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions-messages).
