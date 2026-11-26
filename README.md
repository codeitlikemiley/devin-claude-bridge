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

You need three things: a Devin API key and organization ID for each account, the
keys in your shell environment, and the server registered in Claude Code.

### 1. Get a Devin API key and organization ID

Do this once for each Devin account. Sign in to that account first.

1. Open [app.devin.ai/settings/devin-api?tab=pats](https://app.devin.ai/settings/devin-api?tab=pats).
   In the app this is **Settings → Devin API → PATs**. The link redirects to your
   own organization's page.
2. Copy the **Organization ID** at the top of the page. It starts with `org-`.
3. Click **Create token**.

   ![The Devin API settings page: the Organization ID at the top, the PATs tab and the Create token button](docs/images/devin-api-pats.jpg)

4. Give the token a name such as `claude-bridge`, choose an **Expiration** (the
   default is 30 days), and click **Create token**.

   ![The Create personal access token dialog with a token name and an expiration](docs/images/devin-create-token.jpg)

5. Copy the token straight away. It starts with `cog_` and Devin shows it only once.
   Do not paste it into a command: it would stay in your shell history. Step 2
   puts it in your shell profile and checks it from there.

The screenshots show the dark theme, with the organization name and ID masked.

Things to know about keys:

* **A personal access token (PAT) acts as you.** It works in every organization
  your user can access, so two organizations of one account can share a token
  and differ only in `org_id`. A second Devin login needs its own token.
* **Tokens expire.** When one does, the bridge reports `401` or `403`. Create a
  new token, update the environment variable, and restart Claude Code from a new
  terminal.
* **Enterprise accounts** have PATs off until an admin turns them on, and may
  require approval. See Devin's [PAT guide](https://docs.devin.ai/api-reference/personal-access-tokens).
* **Service users are the alternative**, and the better choice for shared
  automation. On the same page open the **Service users** tab, click
  **Provision service user**, pick the **Member** role and copy the key. With
  custom roles, the service user needs to create, read and message sessions,
  plus `ViewOrgSessions` to list sessions and find Bots. See Devin's
  [authentication guide](https://docs.devin.ai/api-reference/authentication).
* **Legacy keys do not work.** Keys from the **API keys (legacy)** tab start
  with `apk_` and only work with the old v1 and v2 APIs. This bridge uses v3.

### 2. Put the keys in your environment

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then add
the keys to your shell profile (`~/.zshrc`, `~/.bashrc`) and open a new terminal.

**One account** needs no config file. The account is named `default`:

```sh
export DEVIN_API_KEY='cog_your_token'
export DEVIN_ORG_ID='org-your-org'
```

Check the token from the new terminal. The command reads the variable, so the
token is not typed again:

```sh
curl -sS -w '\nHTTP %{http_code}\n' https://api.devin.ai/v3/self \
  -H "Authorization: Bearer $DEVIN_API_KEY"
```

The last line is the result:

* `HTTP 200`, after a JSON description of your user: the token works.
* `HTTP 403` or `HTTP 401`: the token is wrong or expired. Devin answers a bad
  token with `403` and `"detail":"Unauthorized"`.
* A line starting with `curl:` and `HTTP 000`: no response came back from Devin,
  so the token was not checked. Check your network and run the command again.

**Several accounts** need no config file either. Give each account a name and
export one pair of variables with that name as the suffix:

```sh
export DEVIN_API_KEY_WORK='cog_token_for_work'
export DEVIN_ORG_ID_WORK='org-for-work'

export DEVIN_API_KEY_MAX2='cog_token_for_max2'
export DEVIN_ORG_ID_MAX2='org-for-max2'
```

* The suffix in lower case is the account name: these are `work` and `max2`. It
  is the name you say to Claude, so pick one you will recognise. Letters, digits
  and underscores work, so `DEVIN_API_KEY_1` with `DEVIN_ORG_ID_1` is account `1`.
* Both variables of a pair must be set and not empty. Otherwise the bridge stops
  with an error that names the variable.
* The unsuffixed pair (`DEVIN_API_KEY`, `DEVIN_ORG_ID`) can sit beside them as
  the account `default`.

Check each key the same way, with its own variable name in the `curl` command.

**A config file is optional.** You need
`~/.config/devin-claude-bridge/config.toml` only for target aliases, a custom
`base_url`, or keys held under other variable names. Start from
[`config.example.toml`](config.example.toml).

* `[targets.<alias>]` supports `account`, exactly one of `session_id` or
  `bot_service_user_id`, and optional `description`. The account can be one from
  the environment.
* `[accounts.<name>]` supports `org_id`, one of `api_key_env`, `api_key_cmd` or
  `api_key`, and optional `base_url`. `api_key_env` and `api_key_cmd` cannot both
  be set; either one wins over a literal `api_key` and must give a nonempty key.
  File accounts and environment pairs are used
  together; the same name in both is an error. One exception: if a file account
  names `DEVIN_API_KEY_<NAME>` as its `api_key_env`, that variable belongs to the
  file account, and a `DEVIN_ORG_ID_<NAME>` beside it is ignored.
* Never commit real keys; restrict the config with `chmod 600` if it contains
  literal keys. `base_url` receives the bearer key: only use trusted URLs.

**Keys from a command.** `api_key_cmd` fetches the key from a password manager
or the system keychain when the bridge starts, so the key is in no file and in
no environment:

```toml
[accounts.work]
org_id = "org-for-work"
api_key_cmd = "security find-generic-password -a me -s devin-work -w"
```

The command runs without a shell (write it as a string, or as a list of
arguments), has 30 seconds, and must print the key and nothing else. Use this
when the environment is unreliable; see the note on the environment in step 3.
If the same account name is also exported as a `DEVIN_API_KEY_<NAME>` pair, the
bridge stops with a duplicate-account error: remove those exports when you move
an account to `api_key_cmd`.

Config override: `DEVIN_BRIDGE_CONFIG=/absolute/path/config.toml`. An explicit
missing file is an error. When the file defines accounts, the unsuffixed
`DEVIN_API_KEY` pair is ignored; suffixed pairs still count.

### 3. Add the server to Claude Code

**The server gets the environment Claude Code was started with**, which is the
environment of the terminal that started it. A key you export later, a
`/restart` in an old terminal, or a shell that does not re-read your profile all
leave the server without a key, and it then fails to connect. If your keys come
from a profile script, either start Claude Code from a new terminal after every
change, or avoid the environment: use `api_key_cmd` (step 2) and register the
server without `-e`:

```sh
claude mcp add devin-bridge --scope user \
  -- uvx --from git+https://github.com/codeitlikemiley/devin-claude-bridge devin-claude-bridge
```

To pass keys through the environment instead, run this in the terminal where the
keys are set. One account:

```sh
claude mcp add devin-bridge --scope user \
  -e 'DEVIN_API_KEY=${DEVIN_API_KEY}' \
  -e 'DEVIN_ORG_ID=${DEVIN_ORG_ID}' \
  -- uvx --from git+https://github.com/codeitlikemiley/devin-claude-bridge devin-claude-bridge
```

Several accounts: pass each account's pair.

```sh
claude mcp add devin-bridge --scope user \
  -e 'DEVIN_API_KEY_WORK=${DEVIN_API_KEY_WORK}' -e 'DEVIN_ORG_ID_WORK=${DEVIN_ORG_ID_WORK}' \
  -e 'DEVIN_API_KEY_MAX2=${DEVIN_API_KEY_MAX2}' -e 'DEVIN_ORG_ID_MAX2=${DEVIN_ORG_ID_MAX2}' \
  -- uvx --from git+https://github.com/codeitlikemiley/devin-claude-bridge devin-claude-bridge
```

To add an account later, export its pair, then remove the server and add it
again with the extra pair.

* `--scope user` makes the server available in every project. Without it the
  server exists only in the folder where you ran the command.
* Keep the single quotes. They store `${NAME}` as a reference, so the key itself
  is never written to Claude Code's config. Claude Code fills it in from your
  environment each time it starts the server.
* Everything after `--` is the command that starts the server. Its default
  command is `serve` (stdio).

The command prints a line that starts with `Added stdio MCP server devin-bridge`.
Then check the connection:

```sh
claude mcp get devin-bridge
```

It should report `✔ Connected`. Start `claude`, run `/mcp`, and `devin-bridge`
is listed with 10 tools. Ask Claude "list my Devin accounts" to try it.

If it reports `✘ Failed to connect`, run the server's inbox command in the same
terminal. It prints the reason, for example a pair with one variable missing, a
`${NAME}` that was never set, or an `org_id` that does not start with `org-`:

```sh
uvx --from git+https://github.com/codeitlikemiley/devin-claude-bridge devin-claude-bridge inbox
```

After changing the config file, pick **Reconnect** for the server in `/mcp`.
After changing a key, quit Claude Code and start it from a new terminal:
Reconnect reuses the environment Claude Code started with, so it would send the
old key. To remove the server: `claude mcp remove devin-bridge --scope user`.

You can also install with `pip install .` and use `devin-claude-bridge` as the
command after `--`.

### 4. Optional: fewer permission prompts

Claude Code asks before each tool call. The tools are named
`mcp__devin-bridge__<tool>`. To allow the ones that only read, add them to
`~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__devin-bridge__list_accounts",
      "mcp__devin-bridge__list_bots",
      "mcp__devin-bridge__list_sessions",
      "mcp__devin-bridge__read_messages",
      "mcp__devin-bridge__check_inbox",
      "mcp__devin-bridge__wait_for_reply"
    ]
  }
}
```

Leave `create_session` and `send_message` on a prompt: they make Devin work,
which spends your Devin usage.

### Free sessions and billing

`create_session` is off by default, because every session it starts is billed.
Devin's API refuses the web app's **Free (powered by SWE-2)** option: a create
request with `devin_mode: "free-for-teams"` (the web app's name for it) gets a 403,
"Free (powered by SWE-2) is only for sessions a person starts from the webapp,
Slack, Teams, Desktop or the mobile apps." Without a mode, Devin uses the
organization's default, which is a billed mode.

To keep work Free, a person starts each session with **Free** picked, and the
bridge names it with `add_target`. Messages to that session keep its mode. Two ways:

* **By hand:** start the session in the Devin web app, then give Claude its URL:
  “alias https://app.devin.ai/sessions/… as hexuria-mac”.
* **With the `devin-free-session` skill:** Claude Code starts it for you in your
  own signed-in Chrome through Claude in Chrome. It picks Free, the machine
  (Ubuntu, macOS or Windows) and the repositories, sends the prompt, and aliases
  the new session:

  ```sh
  mkdir -p ~/.claude/skills
  ln -s "$PWD/skills/devin-free-session" ~/.claude/skills/devin-free-session
  ```

  ```
  /devin-free-session hexuria macos buwiz-app/books alias=hexuria-mac
  ```

  The skill runs only when you type it, and only on your computer; a Devin
  session cannot use it. Each Devin account is its own login, so Chrome must be
  signed in to that account; the skill asks you to sign in or to connect the
  right Chrome profile. Without a prompt, it sends the
  [worker prompt](skills/devin-free-session/worker-prompt.md).

To allow billed sessions, set `DEVIN_BRIDGE_ALLOW_CREATE=1`. `create_session`
then also needs an explicit `devin_mode`, so it never falls back to the default.
`list_sessions` shows each session's `devin_mode`; it is empty for Free sessions
and for agent previews such as SWE-2 Lightning, which the API does not tell apart.

Two Claude Code limits matter here (see its [MCP docs](https://code.claude.com/docs/en/mcp)):

* A stdio tool call that returns nothing for 30 minutes is stopped. Keep
  `wait_for_reply`'s `timeout_seconds` under 1800; the default is 300.
* A tool result over 25,000 tokens is saved to a file, and Claude reads the file
  when it needs the content.

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
| `list_sessions(account?, active_only=true, limit=20)` | Recent sessions in every account or one, newest activity first: title, status, `devin_mode`, URL, the targets pinned to each, and `child_of` for a session another session started. Active means not archived and not ended; `limit` is per account |
| `create_session(account, prompt, devin_mode, title?, alias?, platform?, repos?, tags?, playbook_id?, secret_ids?, pool?, holder?)` | **Billed, and off by default** (see [Free sessions and billing](#free-sessions-and-billing)). Creates and watches; returns the API session object. With `alias`, the new session becomes that target and a fleet machine, held by `holder` (see [Sharing machines](#sharing-machines-between-claude-sessions)); `pool="neon"` is refused off Linux and beyond four. `platform` picks the machine (`linux`, `macos` or `windows`); an unknown value is refused with the list the account accepts. The other options go to Devin unchanged |
| `add_target(alias, session, account?, description?)` | Names one session, from its Devin URL or ID. Finds the account, refuses an archived or ended session, watches it from now |
| `remove_target(alias, unwatch=true)` | Forgets an alias made with `add_target` |
| `send_message(message, target? / account+session_id, holder?, kind?)` | Sends and watches; returns the updated API session object. Refuses an archived or ended session, refuses after 30 messages to one session in an hour (see [the send limit](#the-send-limit)), and refuses a message the [machine rule](#sharing-machines-between-claude-sessions) does not allow |
| `read_messages(target? / account+session_id, only_new=true, include_user=false)` | Reads every page, returns messages, and advances the shared last-seen position |
| `wait_for_reply(target? / account+session_id, timeout_seconds=300, poll_seconds=15, until_idle=false)` | Waits for Devin's reply and returns it with the session's `status` and `status_detail`. Unread replies from before the call count, and nothing is discarded. `until_idle` waits until Devin stops working |
| `check_inbox(account?)` | New Devin messages across all watched sessions, grouped by account/session, plus a one-time notice for each watched session that was archived, has ended or is gone |
| `watch_session(target? / account+session_id)` | Starts watching without changing an existing cursor |
| `unwatch_session(target? / account+session_id)` | Stops watching and forgets the cursor |
| `fleet(need?, free_only=false)` | One line per machine: kind, Devin's status, free or busy, holder, minutes held, task; then the line of waiting tickets |
| `claim(target, holder, task, follow_up=false, started=false)` | Holds a free machine for a new task |
| `release(target, holder)` | Frees a machine you hold; names the next ticket in line for its kind |
| `queue(need, holder, task)` / `unqueue(ticket, holder)` | Waits in line for a machine of a kind, or leaves the line |
| `describe_target(target, platform, pool, extras="")` | Records a machine's platform and pool (`neon` or `free`) |

### Aliases for sessions

To talk to one session by a short name, give Claude its URL:

* "Alias https://app.devin.ai/sessions/abc123 as hexuria."
* "Message hexuria: rebase on main and run the tests."
* "List my active Devin sessions." Pick one and alias it.
* "Start a session on work to fix the login bug, and make it the work alias."

`add_target` reads the session ID from the URL, asks each configured account for
that session until one has it, and saves the alias. From then on
`target="hexuria"` works everywhere, with no account or session ID to give. A
session that was not watched yet is watched from that moment, so the inbox
shows new replies, not the earlier history; one that was already watched keeps
its unread replies. Aliases are saved in their own file beside the state file
(`state.json.targets.json`), so the MCP server and the `inbox` command share
them; your `config.toml` is never rewritten. An alias in `config.toml` works the
same way but is changed there.

**An archived or ended session is not a valid target.**

* `add_target` refuses one and lists the account's active sessions.
* `send_message` checks the session first. If it is archived or has ended
  (status `exit` or `error`), nothing is sent, and the error says how to re-point
  the alias and lists the active sessions. A suspended session is fine: Devin
  resumes it when the message arrives.
* The inbox reads the status of every watched session before its messages, in
  one request per account. Once it sees a session archived, ended or gone, it
  delivers the last unread replies, stops watching the session, and reports it
  once under `notices`, with the alias if it has one. If that last read fails,
  the watch is kept until a read succeeds, or until Devin says the history is
  gone as well. The same happens after a refused send, and when you move an
  alias away from a dead session.
* The bridge never picks a replacement session by itself. Move the alias with
  `add_target` again.

Targets in `config.toml` are shortcuts, not automatically watched. Watch them
explicitly or send them a message. First reads/watches include existing history; `only_new=false`
replays all history. User messages are excluded by default but still advance the
cursor.

**Waiting for a reply.** `wait_for_reply` returns as soon as a Devin message
follows the latest user message in the session. A reply that arrived before the
call counts, so a fast answer to a message you just sent is not missed, and
older unread replies are returned with it, never dropped. The result carries the
session's `status` and `status_detail`:

* `working`: Devin is still at it; its first message may not be its answer.
* `waiting_for_user`, `waiting_for_approval`, `finished`: Devin has stopped.

In a session where other people or agents also post, their newer message is the
latest user message, so the wait is then for Devin's answer to that one.

Pass `until_idle=true` to keep collecting until Devin has answered and stopped,
and get the whole answer in one call; if Devin already answered and is idle, it
returns at once. If a read fails after replies were collected, they are returned
with `error` and not lost. If the session is archived or ends while you wait, the wait
stops and the result has `ended`. On a timeout, `timed_out` is true and
`messages` holds whatever arrived. Each poll is one message read and one status
read; there is always a last read at the deadline. Timeout is 0–3600 seconds;
poll interval is >0–300 seconds.

### Bot targets that survive recycling

Bots get a new conversation session when recycled or restarted. Prefer their
stable `service-user-...` ID over pinning their current `devin-...` session:

```toml
[targets.research-bot]
account = "max2"
bot_service_user_id = "service-user-your-bot"
description = "Research Bot"
```

Your Bots are listed at [app.devin.ai/settings/bots](https://app.devin.ai/settings/bots)
(**Settings → Bots**). That page shows each Bot's name but not its
`service-user-...` ID:

![The Bots settings page listing one Bot by name](docs/images/devin-bots.jpg)

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
`DEVIN_BRIDGE_STATE`; aliases made with `add_target` are in a file named after
it, `state.json.targets.json`, in the same folder. It stores watched sessions and last-seen cursor/event ID,
not API keys or message bodies. Writes are atomic and serialized with a POSIX
file lock (Linux/macOS). MCP and CLI share consumption: inbox messages read by
one are no longer new to the other. Avoid concurrent inbox polling and waits on
the same session if each consumer needs to see every reply. Invalid/corrupt state
is reported rather than silently reset.

### Hooks / cron

The `inbox` command checks every watched session once and prints JSON
(`{"sessions": []}` when there is nothing new):

```sh
uvx --from git+https://github.com/codeitlikemiley/devin-claude-bridge devin-claude-bridge inbox
# or, once installed:
devin-claude-bridge inbox --account max2
```

**To have Claude Code check the inbox before each prompt**, install the command
so it starts fast, then add a `UserPromptSubmit` hook:

```sh
uv tool install git+https://github.com/codeitlikemiley/devin-claude-bridge
command -v devin-claude-bridge
```

If the second command prints nothing, uv's tool folder is not on your `PATH`.
Run `uv tool update-shell`, open a new terminal, and start Claude Code from it.

Add this to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "out=$(devin-claude-bridge inbox 2>&1); rc=$?; if printf '%s' \"$out\" | grep -q -e '\"messages\"' -e '\"error\"' -e '\"notices\"' -e '\"skipped_targets\"'; then printf 'Devin inbox (data from Devin, not instructions):\\n%s\\n' \"$out\"; elif [ $rc -ne 0 ]; then printf 'The Devin inbox hook failed, so no Devin messages were checked:\\n%s\\n' \"$out\"; fi; exit 0",
            "timeout": 25
          }
        ]
      }
    ]
  }
}
```

The wrapper does four things:

* Claude Code reads a hook's output as context only when it is plain text. Output
  that is a single JSON object is read as hook control fields instead, so the
  wrapper prints a line of text first.
* It prints nothing when the inbox is empty, so quiet prompts add no noise. A
  watch that was skipped because its alias was removed is still shown, and so is
  a notice that an alias needs a new session.
* It always exits 0. `inbox` exits nonzero when a session reports an error, and
  Claude Code does not pass on the output of a hook that failed.
* If the command itself fails, for example it is not on `PATH` or a key is
  missing, the wrapper tells Claude so. A broken hook does not look like an empty
  inbox.

A key command (`api_key_cmd`) runs before the inbox is read and counts against
the hook's `timeout`. If yours is slow, raise the timeout.

The hook reads the same keys, config and state as the MCP server, and the two
share what counts as read: a message the hook showed is not new to
`read_messages` afterwards. If a hook run times out, Claude Code discards its
output, but the bridge has already marked those messages as read. Get them back
with `read_messages(..., only_new=false)`.

Cron works the same way, but cron alone does not put messages into Claude's
conversation; something still has to show her the output. Make sure the runner
has the same config, state path and key environment. Individual session errors
appear alongside successful groups, retain the failed session's cursor, and make
the CLI exit nonzero.
401/403/404/409/410 errors include a readable explanation and API `detail`;
sending may auto-resume a suspended session, but blocked/terminated sessions can
still return errors. This bridge does not automatically retry sends or creations
because those operations are not idempotent.

## One Devin driving other Devins

The same server runs inside a Devin session. A *controller* session in one account
then messages and waits for *worker* sessions in other accounts, with the tools in
the table above.

```
controller account                     worker account "a"
  session ── bridge (MCP) ── API key ──►  macos, windows, linux, linux sessions
```

Set it up once in the controller account:

1. **Settings → Secrets:** add a pair for each worker account, named the way the
   bridge reads them: `DEVIN_API_KEY_A` with `DEVIN_ORG_ID_A`, `DEVIN_API_KEY_B`
   with `DEVIN_ORG_ID_B`, and so on. Get each key and org ID as in
   [step 1](#1-get-a-devin-api-key-and-organization-id), from the worker account.
2. **Settings → MCP → Add custom MCP** (organization), with transport **STDIO**:
   * Command: `uvx`
   * Arguments: `--from git+https://github.com/codeitlikemiley/devin-claude-bridge devin-claude-bridge`
     (add `@<commit>` after the URL to pin a version)
   * Environment variables: each pair from step 1, with its value referring to the
     secret of the same name, such as `$DEVIN_API_KEY_A`.
3. **Check it:** start a new session in the controller account and ask it to call
   two tools:
   * `list_accounts` must name every worker account. An account whose two
     variables are both missing is simply absent, without an error.
   * `list_sessions` must have no `errors` entry: each listed account was asked
     for its sessions and its key worked. An account with no active sessions shows
     no rows, and that is not a failure.

Devin's docs do not show how an MCP environment variable refers to a secret, so
check step 3 before you depend on it. If `uvx` is not on the machine, install
`uv` in the machine setup.

Start the workers in each worker account with **Free** picked and the machine you
want, by hand or with the `devin-free-session` skill on your computer (see
[Free sessions and billing](#free-sessions-and-billing)). Give each worker the
[worker prompt](skills/devin-free-session/worker-prompt.md). Start the controller
the same way in the controller account, with the
[controller prompt](prompts/controller.md) filled in. The controller names the
workers and works with them:

```
list_sessions(account="a")
add_target(alias="a-mac", session="https://app.devin.ai/sessions/…")
send_message("…", target="a-mac")
wait_for_reply(target="a-mac", until_idle=true, timeout_seconds=1800)
```

Aliases and read positions live on the controller's machine, in the state file. A
new controller session starts without them; it can find the workers again with
`list_sessions(account="a")` and `add_target`. Keep the list of work outside the
sessions, for example in GitHub issues, so a new controller can continue it.

### The send limit

Two agents that answer each other can exchange messages without end. The bridge
refuses to send more than 30 messages to one session in an hour, and nothing is
sent when it refuses. The count is kept in its own file next to the state file
(`state.json.sends.json`), so a restart or an unwatch does not clear it. Set `DEVIN_BRIDGE_MAX_SENDS_PER_HOUR` to change the limit; `0` turns it
off.

## Sharing machines between Claude sessions

Several Claude sessions can drive the same Devin machines. Without a record of who
uses which, one session sends a new task to a machine in the middle of another
session's work. The bridge keeps that record and enforces one rule:

* A message that **starts work** goes only to a **free** machine.
* A **busy** machine gets only a **follow-up**, from the session that holds it, on
  the same piece of work. A different job is a new task, even on a machine you hold.
* With no free machine of the kind needed, the task **waits in line**; the first in
  line gets the next machine of that kind that is released.

A machine is every target. It is free when nobody holds it, Devin does not report
it working (status `running`, `claimed` or `resuming`, other than
`waiting_for_user`), and its session is not archived or ended. Machines come in
two kinds: `neon` (Devin with Gemini, Linux only, at most four) for forms work
that needs reading pictures and video, and the others by platform: `linux`,
`macos`, `windows`. A machine nobody described is guessed from its name (`neon…`,
`…macos…`, `…win…`, `…linux…`); `describe_target` sets it.

```
fleet(need="macos", free_only=true)                          # who is free
send_message("Run the macOS CI for PR #5 …", target="mac-a",
             holder="pr-5-session", kind="new")              # claims mac-a, then sends
wait_for_reply(target="mac-a", until_idle=true)              # hint: release when read
send_message("Also run shard 2", target="mac-a",
             holder="pr-5-session", kind="follow_up")
release(target="mac-a", holder="pr-5-session")
queue(need="neon", holder="forms-session", task="check the 1701Q print layout")
```

`holder` is the calling Claude session's name; `kind` is `"new"` or
`"follow_up"`. A `"new"` message is allowed on a machine the holder claimed just
before (the first message after `claim`), or on a free machine with nobody ahead
of the holder in line; the send then claims it, with the message's first line as
the task. A refusal says who holds the machine and for what, lists the free
machines of that kind, and says how to queue; nothing is sent. If a send fails,
the claim it made is undone.

`read_messages` and `wait_for_reply` add a `hint` when a held machine has
finished or waits for the user: release it once you have read the report. Nothing
is released by itself.

**Old callers.** A `send_message` without `holder` and `kind` still works, with a
`warning` that states the rule and says the parameters will become required.
Set `DEVIN_BRIDGE_REQUIRE_HOLDER=1` to refuse such calls instead. Sessions started
before this version hold the old tool schema, so leave it off until they are gone.

The record is `fleet.json` in the state file's folder, shared by every bridge
process under a lock. The same commands work from a shell:

```sh
devin-claude-bridge fleet                       # the table and the line
devin-claude-bridge fleet free macos
devin-claude-bridge fleet claim mac-a my-session "macOS CI for PR #5" [--follow-up|--started]
devin-claude-bridge fleet release mac-a my-session
devin-claude-bridge fleet queue neon my-session "check the 1701Q layout"
devin-claude-bridge fleet unqueue 3 my-session
devin-claude-bridge fleet set neon-1 linux neon ["extras"]
```

A refusal exits with status 2 and the reason on stderr.

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
