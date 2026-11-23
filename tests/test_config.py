import sys

import pytest

from devin_claude_bridge.config import load_config, session_id


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))


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


def pair(monkeypatch, suffix, key="key", org="org-x"):
    monkeypatch.setenv(f"DEVIN_API_KEY_{suffix}", key)
    monkeypatch.setenv(f"DEVIN_ORG_ID_{suffix}", org)


def test_env_pairs_name_accounts(monkeypatch):
    pair(monkeypatch, "WORK", "work-key", "org-work")
    pair(monkeypatch, "MAX2", "max-key", "org-max")
    pair(monkeypatch, "1", "one-key", "org-one")
    config = load_config()
    assert list(config.accounts) == ["1", "max2", "work"]
    assert config.accounts["work"].api_key == "work-key"
    assert config.accounts["work"].org_id == "org-work"
    assert config.accounts["1"].org_id == "org-one"
    assert "work-key" not in repr(config)


def test_env_pairs_sit_beside_the_default_pair(monkeypatch):
    monkeypatch.setenv("DEVIN_API_KEY", "plain")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-plain")
    pair(monkeypatch, "WORK")
    assert sorted(load_config().accounts) == ["default", "work"]
    pair(monkeypatch, "DEFAULT")
    with pytest.raises(ValueError, match="default: defined by DEVIN_API_KEY and"):
        load_config()


@pytest.mark.parametrize(
    "variable,value,error",
    [
        ("DEVIN_API_KEY_WORK", "key", "DEVIN_API_KEY_WORK is set but DEVIN_ORG_ID_WORK is not set"),
        (
            "DEVIN_ORG_ID_WORK",
            "org-x",
            "DEVIN_ORG_ID_WORK is set but DEVIN_API_KEY_WORK is not set",
        ),
    ],
)
def test_half_an_env_pair_is_an_error(monkeypatch, variable, value, error):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=error):
        load_config()


@pytest.mark.parametrize(
    "key,org,error",
    [
        ("", None, "DEVIN_API_KEY_WORK is empty and DEVIN_ORG_ID_WORK is not set"),
        ("", "", "DEVIN_API_KEY_WORK is empty and DEVIN_ORG_ID_WORK is empty"),
        ("key", "", "DEVIN_API_KEY_WORK is set but DEVIN_ORG_ID_WORK is empty"),
        ("", "org-x", "DEVIN_ORG_ID_WORK is set but DEVIN_API_KEY_WORK is empty"),
    ],
)
def test_an_empty_env_pair_is_an_error_beside_a_good_account(monkeypatch, key, org, error):
    pair(monkeypatch, "MAX2")
    monkeypatch.setenv("DEVIN_API_KEY_WORK", key)
    if org is not None:
        monkeypatch.setenv("DEVIN_ORG_ID_WORK", org)
    with pytest.raises(ValueError, match=error):
        load_config()


def test_a_key_the_file_names_does_not_clash_with_another_spelling(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[accounts.legacy]\norg_id = "org-legacy"\napi_key_env = "DEVIN_API_KEY_Work"\n'
    )
    monkeypatch.setenv("DEVIN_BRIDGE_CONFIG", str(path))
    monkeypatch.setenv("DEVIN_API_KEY_Work", "legacy-key")
    monkeypatch.setenv("DEVIN_ORG_ID_Work", "org-ignored")
    pair(monkeypatch, "WORK", "work-key", "org-work")
    config = load_config()
    assert sorted(config.accounts) == ["legacy", "work"]
    assert config.accounts["legacy"].org_id == "org-legacy"
    assert config.accounts["work"].api_key == "work-key"


def test_env_pair_errors(monkeypatch):
    pair(monkeypatch, "WORK", org="work")
    with pytest.raises(ValueError, match="work: org_id must start with org-"):
        load_config()
    pair(monkeypatch, "WORK", key="${DEVIN_API_KEY_WORK}")
    with pytest.raises(ValueError, match="work: API key is the unexpanded text"):
        load_config()
    pair(monkeypatch, "WORK")
    monkeypatch.setenv("DEVIN_ORG_ID_Work", "org-x")
    with pytest.raises(ValueError, match="one spelling"):
        load_config()


def test_env_pairs_merge_with_the_config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("""
[accounts.filed]
org_id = "org-filed"
api_key_env = "DEVIN_API_KEY_FILED"
[targets.bot]
account = "work"
session_id = "abc"
""")
    monkeypatch.setenv("DEVIN_BRIDGE_CONFIG", str(path))
    monkeypatch.setenv("DEVIN_API_KEY_FILED", "filed-key")
    monkeypatch.setenv("DEVIN_API_KEY", "ignored-beside-file-accounts")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-ignored")
    pair(monkeypatch, "WORK", "work-key", "org-work")
    config = load_config()
    assert sorted(config.accounts) == ["filed", "work"]
    assert config.accounts["filed"].org_id == "org-filed"
    assert config.resolve(target="bot") == ("work", "devin-abc")
    monkeypatch.delenv("DEVIN_API_KEY_FILED")
    path.write_text('[accounts.work]\norg_id = "org-file"\napi_key = "k"\n')
    with pytest.raises(ValueError, match="work: defined in the config file and by"):
        load_config()


def test_targets_file_with_env_accounts_only(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[targets.bot]\naccount = "work"\nbot_service_user_id = "service-user-x"\n')
    monkeypatch.setenv("DEVIN_BRIDGE_CONFIG", str(path))
    pair(monkeypatch, "WORK")
    config = load_config()
    assert config.targets["bot"]["account"] == "work"


def cmd_config(monkeypatch, tmp_path, line):
    path = tmp_path / "config.toml"
    path.write_text(f'[accounts.work]\norg_id = "org-work"\n{line}\n')
    monkeypatch.setenv("DEVIN_BRIDGE_CONFIG", str(path))
    return path


def test_api_key_from_a_command(monkeypatch, tmp_path):
    python = sys.executable
    cmd_config(monkeypatch, tmp_path, f'api_key_cmd = "{python} -c \'print(\\"from-cmd\\")\'"')
    config = load_config()
    assert config.accounts["work"].api_key == "from-cmd"
    assert "from-cmd" not in repr(config)
    cmd_config(monkeypatch, tmp_path, f'api_key_cmd = ["{python}", "-c", "print(42)"]')
    assert load_config().accounts["work"].api_key == "42"
    cmd_config(
        monkeypatch,
        tmp_path,
        f'api_key_cmd = ["{python}", "-c", "import sys; print(repr(sys.stdin.read()))"]',
    )
    assert load_config().accounts["work"].api_key == "''"


@pytest.mark.parametrize(
    "code,error",
    [
        ("import sys; print('locked', file=sys.stderr); sys.exit(3)", "exited with 3: locked"),
        ("pass", "must print the key and nothing else"),
        ("print('a'); print('b')", "must print the key and nothing else"),
    ],
)
def test_api_key_command_failures(monkeypatch, tmp_path, code, error):
    cmd_config(monkeypatch, tmp_path, f'api_key_cmd = ["{sys.executable}", "-c", "{code}"]')
    with pytest.raises(ValueError, match=f"work: api_key_cmd {error}"):
        load_config()


def test_api_key_command_config_errors(monkeypatch, tmp_path):
    cmd_config(monkeypatch, tmp_path, 'api_key_cmd = "no-such-command-here get X"')
    with pytest.raises(ValueError, match="command not found: no-such-command-here"):
        load_config()
    cmd_config(monkeypatch, tmp_path, 'api_key_cmd = "x"\napi_key_env = "Y"')
    with pytest.raises(ValueError, match="api_key_env or api_key_cmd, not both"):
        load_config()
    cmd_config(monkeypatch, tmp_path, "api_key_cmd = []")
    with pytest.raises(ValueError, match="missing API key"):
        load_config()


@pytest.mark.parametrize("sid", ["", "../evil", "devin-", "abc?x=1"])
def test_invalid_session(sid):
    with pytest.raises(ValueError, match="Invalid session"):
        session_id(sid)


def test_send_limit_from_environment(monkeypatch):
    monkeypatch.setenv("DEVIN_API_KEY", "secret")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-one")
    assert load_config().max_sends_per_hour == 30
    monkeypatch.setenv("DEVIN_BRIDGE_MAX_SENDS_PER_HOUR", "0")
    assert load_config().max_sends_per_hour == 0
    monkeypatch.setenv("DEVIN_BRIDGE_MAX_SENDS_PER_HOUR", "-5")
    with pytest.raises(ValueError, match="whole number"):
        load_config()


def test_create_is_off_unless_allowed(monkeypatch):
    monkeypatch.setenv("DEVIN_API_KEY", "secret")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-one")
    assert load_config().allow_create is False
    monkeypatch.setenv("DEVIN_BRIDGE_ALLOW_CREATE", "1")
    assert load_config().allow_create is True
    monkeypatch.setenv("DEVIN_BRIDGE_ALLOW_CREATE", "yes")
    with pytest.raises(ValueError, match="0 or 1"):
        load_config()
