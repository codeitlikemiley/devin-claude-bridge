import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def session_id(value: str) -> str:
    value = value.removeprefix("devin-")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid session ID")
    return f"devin-{value}"


DEFAULT_MAX_SENDS_PER_HOUR = 30


def max_sends_per_hour() -> int:
    raw = os.getenv("DEVIN_BRIDGE_MAX_SENDS_PER_HOUR", "").strip()
    if not raw:
        return DEFAULT_MAX_SENDS_PER_HOUR
    if not raw.isdigit():
        raise ValueError(
            f"DEVIN_BRIDGE_MAX_SENDS_PER_HOUR must be a whole number of 0 or more, not {raw!r}"
        )
    return int(raw)


def allow_create() -> bool:
    raw = os.getenv("DEVIN_BRIDGE_ALLOW_CREATE", "").strip()
    if raw not in {"", "0", "1"}:
        raise ValueError(f"DEVIN_BRIDGE_ALLOW_CREATE must be 0 or 1, not {raw!r}")
    return raw == "1"


def require_holder() -> bool:
    """Whether send_message refuses a call that does not name its holder and kind.

    Off for now: Claude sessions started before the fleet tools existed hold the
    old tool schema and cannot pass the new parameters.
    """
    raw = os.getenv("DEVIN_BRIDGE_REQUIRE_HOLDER", "").strip()
    if raw not in {"", "0", "1"}:
        raise ValueError(f"DEVIN_BRIDGE_REQUIRE_HOLDER must be 0 or 1, not {raw!r}")
    return raw == "1"


@dataclass
class Account:
    org_id: str
    api_key: str = field(repr=False)
    base_url: str = "https://api.devin.ai"


@dataclass
class Config:
    accounts: dict[str, Account]
    targets: dict[str, dict]
    # Messages the bridge sends to one session in an hour before it refuses; 0 is no limit.
    max_sends_per_hour: int = DEFAULT_MAX_SENDS_PER_HOUR
    # Whether create_session may start sessions; DEVIN_BRIDGE_ALLOW_CREATE=1 turns it on.
    allow_create: bool = False
    # Whether sends must name holder and kind; DEVIN_BRIDGE_REQUIRE_HOLDER=1 turns it on.
    require_holder: bool = False

    def resolve(self, target=None, account=None, session=None):
        if target:
            if account or session:
                raise ValueError("Use target OR account + session_id, not both")
            if target not in self.targets:
                raise ValueError(f"Unknown target: {target}")
            entry = self.targets[target]
            account, session = entry["account"], entry.get("session_id")
            if entry.get("bot_service_user_id"):
                return account, None
        if account not in self.accounts:
            raise ValueError(f"Unknown account: {account}")
        if not session:
            raise ValueError("Provide target or account + session_id")
        return account, session_id(session)


def key_from_command(name: str, command) -> str:
    """The API key that `api_key_cmd` prints: one line on stdout.

    The command runs without a shell. Its standard input is closed, because the
    bridge's own standard input is the MCP connection.
    """
    argv = shlex.split(command) if isinstance(command, str) else [str(part) for part in command]
    if not argv:
        raise ValueError(f"Account {name}: api_key_cmd is empty")
    try:
        done = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise ValueError(f"Account {name}: api_key_cmd: command not found: {argv[0]}") from None
    except subprocess.TimeoutExpired:
        raise ValueError(f"Account {name}: api_key_cmd took longer than 30 seconds") from None
    except OSError as exc:
        raise ValueError(f"Account {name}: api_key_cmd could not run: {exc}") from None
    if done.returncode:
        said = (done.stderr.strip().splitlines() or [""])[-1][:200]
        raise ValueError(
            f"Account {name}: api_key_cmd exited with {done.returncode}{': ' + said if said else ''}"
        )
    key = done.stdout.strip()
    if not key or "\n" in key:
        raise ValueError(f"Account {name}: api_key_cmd must print the key and nothing else")
    return key


KEY_PREFIX = "DEVIN_API_KEY_"
ORG_PREFIX = "DEVIN_ORG_ID_"


def env_accounts(file_key_envs=()) -> dict[str, dict]:
    """Accounts named by environment pairs: DEVIN_API_KEY_<NAME> with DEVIN_ORG_ID_<NAME>.

    The suffix, in lower case, is the account name. A key variable that the config
    file already names in `api_key_env` belongs to that file account: that exact
    suffix is skipped, key and organization ID both.
    """
    owned = {v[len(KEY_PREFIX) :] for v in file_key_envs if v and v.startswith(KEY_PREFIX)}
    suffixes = {}
    for variable in os.environ:
        for prefix in (KEY_PREFIX, ORG_PREFIX):
            suffix = variable[len(prefix) :]
            if variable.startswith(prefix) and suffix and suffix not in owned:
                suffixes.setdefault(suffix.lower(), set()).add(suffix)
    entries = {}
    for name, spellings in sorted(suffixes.items()):
        if len(spellings) > 1:
            found = ", ".join(sorted(spellings))
            raise ValueError(f"Account {name}: use one spelling of the suffix, found {found}")
        suffix = spellings.pop()
        key_env, org_env = KEY_PREFIX + suffix, ORG_PREFIX + suffix
        if not re.fullmatch(r"[A-Za-z0-9_]+", suffix):
            raise ValueError(f"Account suffix {suffix!r}: use letters, digits and underscores")
        unset = [v for v in (key_env, org_env) if not os.getenv(v)]
        states = [f"{v} is {'empty' if v in os.environ else 'not set'}" for v in unset]
        if len(unset) == 2:
            raise ValueError(f"Account {name}: {' and '.join(states)}")
        if unset:
            present = org_env if unset[0] == key_env else key_env
            raise ValueError(f"Account {name}: {present} is set but {states[0]}")
        entries[name] = {"org_id": os.environ[org_env], "api_key_env": key_env}
    return entries


def load_config() -> Config:
    override = os.getenv("DEVIN_BRIDGE_CONFIG")
    path = Path(override or "~/.config/devin-claude-bridge/config.toml").expanduser()
    if override or path.exists():
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    else:
        data = {}
    entries = dict(data.get("accounts", {}))
    discovered = env_accounts({e.get("api_key_env") for e in entries.values()})
    for name in discovered:
        if name in entries:
            raise ValueError(
                f"Account {name}: defined in the config file and by DEVIN_API_KEY_* variables"
            )
    if not entries and (os.getenv("DEVIN_API_KEY") or os.getenv("DEVIN_ORG_ID")):
        if "default" in discovered:
            raise ValueError(
                "Account default: defined by DEVIN_API_KEY and by DEVIN_API_KEY_DEFAULT"
            )
        entries["default"] = {
            "org_id": os.getenv("DEVIN_ORG_ID", ""),
            "api_key_env": "DEVIN_API_KEY",
        }
    entries.update(discovered)
    accounts = {}
    for name, entry in entries.items():
        if entry.get("api_key_env") and entry.get("api_key_cmd"):
            raise ValueError(f"Account {name}: set api_key_env or api_key_cmd, not both")
        if entry.get("api_key_cmd"):
            key = key_from_command(name, entry["api_key_cmd"])
        elif entry.get("api_key_env"):
            key = os.getenv(entry["api_key_env"])
        else:
            key = entry.get("api_key")
        if not key:
            raise ValueError(
                f"Account {name}: missing API key; set api_key_env, api_key_cmd or api_key"
            )
        org = entry.get("org_id", "")
        for label, value in (("API key", key), ("org_id", org)):
            if value.startswith("${"):
                raise ValueError(
                    f"Account {name}: {label} is the unexpanded text {value}; "
                    "set that variable in the shell that starts Claude Code"
                )
        if not re.fullmatch(r"org-[A-Za-z0-9_-]+", org):
            raise ValueError(f"Account {name}: org_id must start with org-")
        accounts[name] = Account(org, key, entry.get("base_url", "https://api.devin.ai"))
    if not accounts:
        raise ValueError(
            "No accounts configured; set DEVIN_API_KEY and DEVIN_ORG_ID, "
            "DEVIN_API_KEY_<NAME> and DEVIN_ORG_ID_<NAME> pairs, or config.toml"
        )
    targets = {}
    for name, entry in data.get("targets", {}).items():
        if entry.get("account") not in accounts:
            raise ValueError(f"Target {name}: unknown account")
        if bool(entry.get("session_id")) == bool(entry.get("bot_service_user_id")):
            raise ValueError(f"Target {name}: set exactly one of session_id or bot_service_user_id")
        bot = entry.get("bot_service_user_id")
        if bot and not re.fullmatch(r"service-user-[A-Za-z0-9_-]+", bot):
            raise ValueError(f"Target {name}: bot_service_user_id must start with service-user-")
        targets[name] = {
            "account": entry["account"],
            **(
                {"bot_service_user_id": bot}
                if bot
                else {"session_id": session_id(entry["session_id"])}
            ),
            "description": entry.get("description", ""),
        }
    return Config(accounts, targets, max_sends_per_hour(), allow_create(), require_holder())
