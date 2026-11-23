---
name: devin-free-session
description: Start a Free (SWE-2) Devin session in the Devin web app through Claude in Chrome, on a chosen machine and repositories, then alias it with the devin-bridge MCP.
disable-model-invocation: true
argument-hint: <account> [ubuntu|macos|windows] [owner/repo ...] [alias=<name>] [prompt=<text>]
---

# Start a Free Devin session

Devin's API refuses Free sessions with a 403: "Free (powered by SWE-2) is only for
sessions a person starts from the webapp, Slack, Teams, Desktop or the mobile
apps." Every session the API starts is billed. This skill starts the session the
way a person does, in the person's own signed-in browser, on their request, and
then hands it to the bridge. It runs on the person's computer only; a Devin
session cannot use it.

When the person already has a Free session, skip this skill: call
`add_target(alias, <session URL>)` with the URL.

## Inputs

Read them from the arguments; ask once for anything missing that has no default.

- **account**: a bridge account name from `list_accounts`. Required.
- **machine**: `ubuntu` (default), `macos` or `windows`.
- **repositories**: zero or more `owner/name`. Exactly these end up ticked.
- **alias**: default `<account>-<machine>`; add `-2`, `-3` when that alias is taken.
- **prompt**: default the worker prompt in [worker-prompt.md](worker-prompt.md),
  with `<ALIAS>` replaced.

## Steps

Use the Claude in Chrome tools. Find controls with `find` or `read_page` by the
labels below; take coordinates from a fresh screenshot only when a label is
missing. Each step ends on its check.

1. **Account.** Call `list_accounts`. Check: the account is listed.
2. **Browser.** Open a new tab at `https://app.devin.ai/`. The sidebar's account
   button (bottom left) shows the signed-in account. Each Devin account is its own
   login, usually in its own Chrome profile. If the button shows another account,
   or a sign-in page appears, stop and ask the person to sign in, or to connect the
   Chrome profile that is signed in to that account (`switch_browser`). The person
   types passwords and codes. Check: the account button shows the account.
3. **Free slot.** Open the mode button beside the microphone (it shows the
   current mode, such as `Normal`) and hover **Free**. Its tooltip reads
   "Free (powered by SWE-2) … N/4 in use". Check: N is below 4. At 4/4, stop and
   report that every Free slot is busy.
4. **Free mode.** Click **Free**. Check: the mode button reads `Free`.
5. **Machine.** Open **Configuration** (the icon button beside "Commands and
   skills") → **Machine** → under **Hosted**, pick **Ubuntu**, **macOS** or
   **Windows**. Check: the Machine row shows that machine.
6. **Repositories.** In the same popover, open **Notable repositories**. It has a
   search box and one checkbox per repository, grouped by owner, and it can come
   with repositories already ticked. Tick each requested repository (search for
   it by name) and untick every other one. Check: the row reads
   "<count> selected" and the ticked set equals the request. Close the popover
   with Escape.
7. **Prompt.** Type the prompt into the **Prompt** box. Check, right before
   sending: the mode button still reads `Free`, and the screenshot shows the prompt.
8. **Send.** Click **Send**. The tab moves to `https://app.devin.ai/sessions/<id>`.
   Check: the URL has a session ID. Copy that URL.
9. **Alias.** Call `add_target(alias, <session URL>, account)`. Check: it returns
   the session with that account.
10. **Mode check.** Call `list_sessions(account=<account>)` and find the session's
    row. Check: `devin_mode` is empty, which is how the API shows a Free session.
    A value such as `normal` means the session is billed: tell the person at once,
    with the URL, so they can stop it in the web app.

Close the tab you opened. Report the alias, URL, machine, repositories and the
Free slot count.

## When something differs

The web app changes. When a label or menu is not where these steps say, take a
screenshot, find the control by what it does, and say what changed in the report.
When two attempts at the same control fail, stop and ask the person.
