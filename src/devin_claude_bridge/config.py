import os
import re
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


@dataclass
class Account:
    org_id: str
    api_key: str = field(repr=False)
    base_url: str = "https://api.devin.ai"


@dataclass
class Config:
    accounts: dict[str, Account]
    targets: dict[str, dict]

    def resolve(self, target=None, account=None, session=None):
        if target:
            if account or session:
                raise ValueError("Use target OR account + session_id, not both")
            if target not in self.targets:
                raise ValueError(f"Unknown target: {target}")
            entry = self.targets[target]
            account, session = entry["account"], entry["session_id"]
        if account not in self.accounts:
            raise ValueError(f"Unknown account: {account}")
        if not session:
            raise ValueError("Provide target or account + session_id")
        return account, session_id(session)


def load_config() -> Config:
    override = os.getenv("DEVIN_BRIDGE_CONFIG")
    path = Path(override or "~/.config/devin-claude-bridge/config.toml").expanduser()
    if override or path.exists():
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    else:
        data = {}
    entries = data.get("accounts", {})
    if not entries and (os.getenv("DEVIN_API_KEY") or os.getenv("DEVIN_ORG_ID")):
        entries = {
            "default": {"org_id": os.getenv("DEVIN_ORG_ID", ""), "api_key_env": "DEVIN_API_KEY"}
        }
    accounts = {}
    for name, entry in entries.items():
        key = os.getenv(entry["api_key_env"]) if entry.get("api_key_env") else entry.get("api_key")
        if not key:
            raise ValueError(f"Account {name}: missing API key; set api_key_env or api_key")
        org = entry.get("org_id", "")
        if not re.fullmatch(r"org-[A-Za-z0-9_-]+", org):
            raise ValueError(f"Account {name}: org_id must start with org-")
        accounts[name] = Account(org, key, entry.get("base_url", "https://api.devin.ai"))
    if not accounts:
        raise ValueError(
            "No accounts configured; set DEVIN_API_KEY and DEVIN_ORG_ID or config.toml"
        )
    targets = {}
    for name, entry in data.get("targets", {}).items():
        if entry.get("account") not in accounts:
            raise ValueError(f"Target {name}: unknown account")
        targets[name] = {
            "account": entry["account"],
            "session_id": session_id(entry.get("session_id", "")),
            "description": entry.get("description", ""),
        }
    return Config(accounts, targets)
