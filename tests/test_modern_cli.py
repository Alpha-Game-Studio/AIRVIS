from __future__ import annotations

import json


def test_modern_help_and_version(capsys):
    from airvis.modern_cli import main

    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    version = capsys.readouterr().out
    assert version.startswith("airvis 8.2")


def test_modern_platforms_agent_json(capsys):
    from airvis.modern_cli import main

    assert main(["--agent", "platforms"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "openrouter" in payload["providers"]
    assert "voice" in payload["channels"]
    assert "elevenlabs" in payload["voice"]["tts"]


def test_modern_guide(capsys):
    from airvis.modern_cli import main

    assert main(["guide"]) == 0
    output = capsys.readouterr().out
    assert "airvis init" in output
    assert "airvis actions" in output
    assert "airvis voice" in output


def test_modern_list_uninitialized(monkeypatch, tmp_path, capsys):
    import airvis.modern_cli as cli

    monkeypatch.setattr(cli, "SETUP_PATH", tmp_path / "setup.json")
    assert cli.main(["--agent", "list"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["initialized"] is False


def test_modern_model_list(monkeypatch, tmp_path, capsys):
    import airvis.modern_cli as cli

    setup = tmp_path / "setup.json"
    setup.write_text(json.dumps({"default_provider": "openrouter", "model": "openai/gpt-5-mini"}), encoding="utf-8")
    monkeypatch.setattr(cli, "SETUP_PATH", setup)
    assert cli.main(["--agent", "model", "list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "openrouter"
    assert payload["model"] == "openai/gpt-5-mini"
