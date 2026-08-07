import subprocess
from pathlib import Path

import pytest

from doc2md.core import Document, GitCheckout, Repository


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout


def test_git_checkout_commits_and_pushes_configured_output_root(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    remote.mkdir()
    seed.mkdir()
    git("init", "--bare", cwd=remote)
    git("init", "-b", "main", cwd=seed)
    git("config", "user.name", "Test", cwd=seed)
    git("config", "user.email", "test@example.invalid", cwd=seed)
    git("config", "commit.gpgsign", "false", cwd=seed)
    (seed / "README.md").write_text("# Documents\n")
    git("add", "README.md", cwd=seed)
    git("commit", "-m", "Initial commit", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "-u", "origin", "main", cwd=seed)

    checkout = GitCheckout(str(remote), output_root=Path("generated"))
    with checkout as path:
        Repository(path, output_root=Path("generated")).apply(
            "notion",
            [
                Document(
                    source="notion",
                    source_id="page-1",
                    title="Page",
                    body="Body",
                    updated_at="2026-01-01T00:00:00Z",
                )
            ],
        )
        assert checkout.commit_and_push("Sync documents")

    tree = git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main", cwd=tmp_path)
    assert "generated/notion/page--page1.md" in tree
    assert "generated/.doc2md/manifest.json" in tree


def test_git_checkout_rejects_header_for_non_http_remote(tmp_path: Path) -> None:
    from doc2md.core import Doc2mdError

    try:
        GitCheckout(str(tmp_path / "remote.git"), auth_header="Authorization: Bearer token")
    except Doc2mdError as exc:
        assert "HTTP or HTTPS" in str(exc)
    else:
        raise AssertionError("expected non-HTTP authenticated remote to be rejected")


def test_git_auth_header_is_not_put_in_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", run)
    checkout = GitCheckout(
        "https://example.invalid/documents.git",
        auth_header="Authorization: Bearer test-secret",
    )
    checkout.path = tmp_path

    checkout._git("status")

    assert "test-secret" not in repr(captured["command"])
    assert captured["environment"]["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer test-secret"
