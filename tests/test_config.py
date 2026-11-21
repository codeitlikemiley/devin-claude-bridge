import pytest

from devin_claude_bridge.config import load_config, session_id


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("DEVIN_BRIDGE_CONFIG", "DEVIN_API_KEY", "DEVIN_ORG_ID"):
        monkeypatch.delenv(name, raising=False)


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("DEVIN_API_KEY", "secret")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-one")
    config = load_config()
    assert list(config.accounts) == ["default"]
    assert "secret" not in repr(config)
    assert config.resolve(account="default", session="abc") == ("default", "devin-abc")


def test_multi_account(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("""
[accounts.max2]
org_id = "org-one"
api_key_env = "KEY_ONE"
api_key = "ignored"
[accounts.work]
org_id = "org-two"
api_key = "literal"
base_url = "https://example.com"
[targets.research-bot]
account = "max2"
session_id = "abc"
description = "Research"
""")
    monkeypatch.setenv("DEVIN_BRIDGE_CONFIG", str(path))
    monkeypatch.setenv("KEY_ONE", "environment-key")
    config = load_config()
    assert config.accounts["max2"].api_key == "environment-key"
    assert config.accounts["work"].base_url == "https://example.com"
    assert config.resolve(target="research-bot") == ("max2", "devin-abc")
    with pytest.raises(ValueError, match="OR"):
        config.resolve(target="research-bot", account="max2")
    with pytest.raises(ValueError, match="Unknown target"):
        config.resolve(target="missing")
    with pytest.raises(ValueError, match="Unknown account"):
        config.resolve(account="missing", session="x")
    monkeypatch.delenv("KEY_ONE")
    with pytest.raises(ValueError, match="missing API key"):
        load_config()


@pytest.mark.parametrize(
    "body,error",
    [
        ('[accounts.x]\norg_id="org-x"', "missing API key"),
        ('[accounts.x]\norg_id="x"\napi_key="k"', "org_id"),
        (
            (
                '[accounts.x]\norg_id="org-x"\napi_key="k"\n'
                '[targets.bot]\naccount="missing"\nsession_id="x"'
            ),
            "unknown account",
        ),
    ],
)
def test_invalid_config(monkeypatch, tmp_path, body, error):
    path = tmp_path / "config.toml"
    path.write_text(body)
    monkeypatch.setenv("DEVIN_BRIDGE_CONFIG", str(path))
    with pytest.raises(ValueError, match=error):
        load_config()


def test_no_config_and_missing_override(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="No accounts"):
        load_config()
    monkeypatch.setenv("DEVIN_BRIDGE_CONFIG", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError):
        load_config()


@pytest.mark.parametrize("sid", ["", "../evil", "devin-", "abc?x=1"])
def test_invalid_session(sid):
    with pytest.raises(ValueError, match="Invalid session"):
        session_id(sid)
