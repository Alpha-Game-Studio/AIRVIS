from __future__ import annotations

import json


def test_render_status(capsys):
    from airvis.launcher import render

    render({"providers": ["openai", "ollama"], "backends": ["native", "openclaw"], "agents": ["coder"], "tools": ["read_file"]}, "status")
    out = capsys.readouterr().out
    assert "AIRVIS 8.2" in out
    assert "Providers" in out
    assert "2 registered" in out


def test_launcher_preserves_explicit_json(monkeypatch):
    import airvis.launcher as launcher

    called = {}

    def fake_main(args):
        called["args"] = args
        return 0

    monkeypatch.setattr("airvis.cli.main", fake_main)
    assert launcher.main(["--json", "status"]) == 0
    assert called["args"] == ["--json", "status"]


def test_setup_writes_engine_and_metadata(monkeypatch, tmp_path):
    import airvis.setup as setup

    metadata = tmp_path / "setup.json"
    engine_config = tmp_path / "airvis.json"
    monkeypatch.setattr(setup, "SETUP_PATH", metadata)
    monkeypatch.setattr(setup, "WORKSPACE_CONFIG", engine_config)
    answers = iter(["7", "1", "1", "1", "", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    assert setup.run() == 0
    setup_data = json.loads(metadata.read_text(encoding="utf-8"))
    config = json.loads(engine_config.read_text(encoding="utf-8"))
    assert setup_data["provider"] == "mock"
    assert setup_data["channels"] == ["cli"]
    assert config["providers"]["default"] == "mock"
    assert config["routing"]["strategy"] == "balanced"
