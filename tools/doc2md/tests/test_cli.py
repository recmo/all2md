import argparse
import json
from pathlib import Path

import pytest

from doc2md.cli import (
    _destination_context,
    _load_config,
    _output_root,
    _provider,
    _secret,
    run_doctor,
)
from doc2md.core import Doc2mdError


def write_config(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value))
    return path


def test_local_dry_run_uses_disposable_copy(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    original = output / "keep.md"
    original.write_text("original")

    with _destination_context(
        output,
        {},
        output_root=Path("sources"),
        dry_run=True,
    ) as (preview, checkout):
        assert checkout is None
        (preview / "keep.md").write_text("changed")
        (preview / "new.md").write_text("preview")

    assert original.read_text() == "original"
    assert not (output / "new.md").exists()


def test_local_directory_does_not_require_git_configuration(tmp_path: Path) -> None:
    with _destination_context(tmp_path, {}, output_root=Path("sources")) as (output, checkout):
        assert output == tmp_path.resolve()
        assert checkout is None


def test_destination_requires_directory_or_git_remote() -> None:
    with pytest.raises(Doc2mdError, match="provide --directory"):
        with _destination_context(None, {}, output_root=Path("sources")):
            pass


def test_secret_can_be_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC2MD_TEST_TOKEN", "secret")

    assert _secret({"env": "DOC2MD_TEST_TOKEN"}, "notion.token") == "secret"


def test_missing_secret_environment_variable_is_clear() -> None:
    with pytest.raises(Doc2mdError, match="is not set"):
        _secret({"env": "DOC2MD_MISSING_TOKEN"}, "notion.token")


def test_malformed_configuration_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path / "config.json", ["not", "an", "object"])

    with pytest.raises(Doc2mdError, match="JSON object"):
        _load_config(path)


def test_output_root_must_be_relative() -> None:
    with pytest.raises(Doc2mdError, match="safe relative"):
        _output_root({"output": {"root": "../outside"}})


def test_disabled_provider_is_not_constructed() -> None:
    assert _provider("notion", {"notion": {"enabled": False}}, Path("sources")) is None


def test_doctor_reports_missing_credentials_and_roots(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "config.json",
        {
            "notion": {"enabled": True},
            "google_docs": {"enabled": True, "bearer": "token", "root_ids": []},
        },
    )

    result = run_doctor(argparse.Namespace(config=path, directory=None))

    assert not result["ok"]
    assert "notion.token is required" in result["issues"]
    assert "google_docs.root_ids must be a non-empty list of strings" in result["issues"]


def test_doctor_accepts_minimal_local_configuration(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "config.json",
        {"notion": {"enabled": True, "token": "token"}},
    )

    assert run_doctor(argparse.Namespace(config=path, directory=tmp_path)) == {
        "ok": True,
        "issues": [],
    }


def test_doctor_requires_a_local_or_git_destination(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "config.json",
        {"notion": {"enabled": True, "token": "token"}},
    )

    result = run_doctor(argparse.Namespace(config=path, directory=None))

    assert "provide --directory or configure git.remote" in result["issues"]


def test_doctor_rejects_non_boolean_enabled_and_invalid_base_url(tmp_path: Path) -> None:
    enabled_path = write_config(
        tmp_path / "enabled.json",
        {"notion": {"enabled": "false", "token": "token"}},
    )
    url_path = write_config(
        tmp_path / "url.json",
        {"notion": {"enabled": True, "token": "token", "base_url": []}},
    )

    enabled = run_doctor(argparse.Namespace(config=enabled_path, directory=tmp_path))
    url = run_doctor(argparse.Namespace(config=url_path, directory=tmp_path))

    assert "notion.enabled must be a boolean" in enabled["issues"]
    assert "notion.base_url must be a non-empty string" in url["issues"]


def test_sync_rejects_malformed_provider_configuration() -> None:
    with pytest.raises(Doc2mdError, match="enabled must be a boolean"):
        _provider(
            "notion",
            {"notion": {"enabled": "false", "token": "token"}},
            Path("sources"),
        )
