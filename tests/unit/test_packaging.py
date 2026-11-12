"""Packaging/install glue: CLI argv handling and panel asset resolution."""
from __future__ import annotations

import importlib

import pytest

from makemeswitch.main import _normalise_argv


def test_bare_invocation_runs_the_cli() -> None:
    assert _normalise_argv([]) == []


def test_flags_are_routed_to_the_cli() -> None:
    assert _normalise_argv(["-c", "show version"]) == ["cli", "-c", "show version"]
    assert _normalise_argv(["--no-banner"]) == ["cli", "--no-banner"]


def test_real_subcommands_are_left_alone() -> None:
    for sub in ("serve", "init", "doctor", "user"):
        assert _normalise_argv([sub]) == [sub]
    assert _normalise_argv(["--version"]) == ["--version"]


def test_web_root_honours_env_override(tmp_path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><title>MakeMeSwitch Manager</title></html>")
    monkeypatch.setenv("MMS_WEB_DIST", str(dist))

    import makemeswitch.api.server as server

    reloaded = importlib.reload(server)
    try:
        assert reloaded.WEB_ROOT == dist
        assert (reloaded.WEB_ROOT / "index.html").is_file()
    finally:
        monkeypatch.delenv("MMS_WEB_DIST", raising=False)
        importlib.reload(server)


def test_web_root_falls_back_to_package(monkeypatch) -> None:
    monkeypatch.delenv("MMS_WEB_DIST", raising=False)
    monkeypatch.delenv("MMS_APP_DIR", raising=False)

    import makemeswitch.api.server as server

    reloaded = importlib.reload(server)
    assert reloaded.WEB_ROOT.name == "dist"
    assert "makemeswitch" in str(reloaded.WEB_ROOT)


@pytest.mark.parametrize("value", ["cli", "serve"])
def test_normalise_keeps_existing_flags(value: str) -> None:
    assert _normalise_argv([value, "--help"])[0] == value
