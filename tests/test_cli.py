import json
from unittest.mock import AsyncMock, patch

import pytest

from devin_claude_bridge.cli import main


def test_inbox_empty(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DEVIN_BRIDGE_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("DEVIN_BRIDGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("DEVIN_API_KEY", "fake-key")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-test")
    (tmp_path / "config.toml").write_text("")
    monkeypatch.setattr("sys.argv", ["devin-claude-bridge", "inbox"])
    main()
    assert json.loads(capsys.readouterr().out) == {"sessions": []}


def test_inbox_errors_exit_nonzero(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["devin-claude-bridge", "inbox", "--account", "one"])
    result = {"sessions": [{"account": "one", "session_id": "devin-a", "error": "403 denied"}]}
    with (
        patch("devin_claude_bridge.cli.load_config"),
        patch("devin_claude_bridge.cli.Bridge") as bridge,
    ):
        bridge.return_value.check_inbox = AsyncMock(return_value=result)
        with pytest.raises(SystemExit, match="1"):
            main()
        bridge.return_value.check_inbox.assert_awaited_once_with("one")
    assert json.loads(capsys.readouterr().out) == result


def test_config_error_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["devin-claude-bridge"])
    with (
        patch("devin_claude_bridge.cli.load_config", side_effect=ValueError("missing key")),
        pytest.raises(SystemExit, match="1"),
    ):
        main()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "missing key" in captured.err
