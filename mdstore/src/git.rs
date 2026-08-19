use std::{
    collections::BTreeSet,
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
};

use anyhow::{Context, Result, bail};
use serde::Serialize;

use crate::config::{Config, ensure_repository_path_safe, validate_repo_path};

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PushState {
    Disabled,
    Pushed,
    Queued,
    Diverged,
}

pub fn ensure_repository(root: &Path) -> Result<()> {
    let output = run(root, ["rev-parse", "--is-inside-work-tree"])?;
    if !output.status.success() || String::from_utf8_lossy(&output.stdout).trim() != "true" {
        bail!("{} is not a Git worktree", root.display());
    }
    Ok(())
}

pub fn git_dir(root: &Path) -> Result<PathBuf> {
    let output = checked(root, ["rev-parse", "--git-dir"])?;
    let path = PathBuf::from(String::from_utf8(output.stdout)?.trim());
    Ok(if path.is_absolute() {
        path
    } else {
        root.join(path)
    })
}

pub fn tracked_markdown(root: &Path, config: &Config) -> Result<Vec<String>> {
    let output = checked(root, ["ls-files", "-z", "--", "*.md"])?;
    let (include, exclude) = config.document_globs()?;
    Ok(output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8_lossy(part).into_owned())
        .filter(|path| include.is_match(path) && !exclude.is_match(path))
        .collect())
}

pub fn tracked_config_files(root: &Path) -> Result<Vec<String>> {
    let output = checked(root, ["ls-files", "-z", "--", ".mdstore/**"])?;
    Ok(output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8_lossy(part).into_owned())
        .filter(|path| {
            Path::new(path).extension().is_some_and(|extension| {
                extension.eq_ignore_ascii_case("yaml")
                    || extension.eq_ignore_ascii_case("yml")
                    || extension.eq_ignore_ascii_case("json")
            })
        })
        .collect())
}

pub fn is_tracked(root: &Path, path: &str) -> Result<bool> {
    Ok(run(root, ["ls-files", "--error-unmatch", "--", path])?
        .status
        .success())
}

pub fn is_ignored(root: &Path, path: &str) -> Result<bool> {
    Ok(run(root, ["check-ignore", "--quiet", "--", path])?
        .status
        .success())
}

pub fn ensure_ignored<'a>(root: &Path, paths: impl IntoIterator<Item = &'a str>) -> Result<()> {
    let paths: BTreeSet<&str> = paths.into_iter().collect();
    if paths.is_empty() {
        return Ok(());
    }
    for path in &paths {
        validate_repo_path(path)?;
    }
    let mut input = Vec::new();
    for path in &paths {
        input.extend_from_slice(path.as_bytes());
        input.push(0);
    }
    let mut child = Command::new("git")
        .current_dir(root)
        .args(["check-ignore", "--stdin", "-z"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("check derived paths against Git ignore rules")?;
    let mut stdin = child.stdin.take().context("open git check-ignore input")?;
    let writer = std::thread::spawn(move || stdin.write_all(&input));
    let output = child.wait_with_output()?;
    let write_result = writer
        .join()
        .map_err(|_| anyhow::anyhow!("git check-ignore input writer panicked"))?;
    if !output.status.success() && output.status.code() != Some(1) {
        bail!(
            "git check-ignore failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    write_result.context("write git check-ignore input")?;
    let ignored: BTreeSet<String> = output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8(part.to_vec()))
        .collect::<std::result::Result<_, _>>()?;
    let missing: Vec<&str> = paths
        .into_iter()
        .filter(|path| !ignored.contains(*path))
        .collect();
    if !missing.is_empty() {
        bail!("derived paths must be ignored and untracked: {missing:?}");
    }
    Ok(())
}

pub fn head(root: &Path) -> Result<String> {
    let output = checked(root, ["rev-parse", "HEAD"])?;
    Ok(String::from_utf8(output.stdout)?.trim().into())
}

pub fn read_head_text(root: &Path, path: &str) -> Result<String> {
    validate_repo_path(path)?;
    let object = format!("HEAD:{path}");
    let output = checked(root, ["show", &object])?;
    String::from_utf8(output.stdout).context("committed repository file is not UTF-8")
}

pub fn recover_worktree(root: &Path) -> Result<Vec<String>> {
    let output = checked(root, ["diff", "HEAD", "--name-only", "-z"])?;
    let modified: Vec<String> = output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8_lossy(part).into_owned())
        .collect();
    if !modified.is_empty() {
        let mut command = Command::new("git");
        command
            .current_dir(root)
            .args(["restore", "--staged", "--worktree", "--"]);
        command.args(&modified);
        let output = command
            .output()
            .context("restore daemon-owned tracked files")?;
        if !output.status.success() {
            bail!(
                "git restore failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }
    quarantine_untracked_markdown(root)?;
    Ok(modified)
}

fn quarantine_untracked_markdown(root: &Path) -> Result<()> {
    let output = checked(
        root,
        [
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "*.md",
        ],
    )?;
    let paths: Vec<String> = output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8_lossy(part).into_owned())
        .collect();
    if paths.is_empty() {
        return Ok(());
    }
    let quarantine = create_quarantine_directory(root)?;
    for path in paths {
        validate_repo_path(&path)?;
        let source = root.join(&path);
        let target = quarantine.join(&path);
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::rename(&source, &target)
            .with_context(|| format!("quarantine untracked Markdown {path}"))?;
    }
    Ok(())
}

fn create_quarantine_directory(root: &Path) -> Result<PathBuf> {
    let parent = git_dir(root)?.join("mdstore/quarantine");
    fs::create_dir_all(&parent)?;
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_secs();
    for suffix in 0_u64.. {
        let candidate = parent.join(format!("{timestamp}-{suffix}"));
        match fs::create_dir(&candidate) {
            Ok(()) => return Ok(candidate),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error.into()),
        }
    }
    unreachable!()
}

pub fn write_changes(root: &Path, changes: &[(String, Option<String>)]) -> Result<()> {
    for (path, content) in changes {
        validate_repo_path(path)?;
        let target = root.join(path);
        ensure_repository_path_safe(root, path)?;
        if let Some(content) = content {
            let parent = target.parent().context("target has no parent")?;
            fs::create_dir_all(parent)?;
            let mut temp = tempfile::NamedTempFile::new_in(parent)?;
            temp.write_all(content.as_bytes())?;
            temp.as_file().sync_all()?;
            temp.persist(&target).map_err(|error| error.error)?;
        } else if target.exists() {
            fs::remove_file(&target)?;
        }
    }
    Ok(())
}

pub fn stage_tree(root: &Path, paths: &[String]) -> Result<Option<String>> {
    let mut add = Command::new("git");
    add.current_dir(root).args(["add", "-A", "--"]).args(paths);
    let output = add.output().context("stage mdstore edit batch")?;
    if !output.status.success() {
        bail!(
            "git add failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    let staged = run(root, ["diff", "--cached", "--quiet"])?;
    if staged.status.success() {
        return Ok(None);
    }
    let tree = checked(root, ["write-tree"])?;
    Ok(Some(String::from_utf8(tree.stdout)?.trim().into()))
}

pub fn commit_tree(root: &Path, tree: &str, parent: &str, summary: &str) -> Result<String> {
    let reference = checked(root, ["symbolic-ref", "-q", "HEAD"])?;
    let reference = String::from_utf8(reference.stdout)?.trim().to_owned();
    let mut child = Command::new("git")
        .current_dir(root)
        .args(["commit-tree", tree, "-p", parent, "-F", "-"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("create mdstore commit from validated tree")?;
    child
        .stdin
        .take()
        .context("open git commit message input")?
        .write_all(summary.as_bytes())?;
    let output = child.wait_with_output()?;
    if !output.status.success() {
        bail!(
            "git commit-tree failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    let commit = String::from_utf8(output.stdout)?.trim().to_owned();
    let output = run(
        root,
        [
            "update-ref",
            "-m",
            "mdstore commit",
            &reference,
            &commit,
            parent,
        ],
    )?;
    if !output.status.success() {
        bail!(
            "git update-ref failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    Ok(commit)
}

pub fn history_contains_tree(root: &Path, base: &str, tree: &str) -> Result<bool> {
    let ancestor = run(root, ["merge-base", "--is-ancestor", base, "HEAD"])?;
    if !ancestor.status.success() {
        return Ok(false);
    }
    let range = format!("{base}..HEAD");
    let output = checked(root, ["log", "--format=%T", &range])?;
    Ok(String::from_utf8(output.stdout)?
        .lines()
        .any(|candidate| candidate == tree))
}

pub fn rollback(root: &Path, paths: &[String]) -> Result<()> {
    let mut present = Vec::new();
    let mut absent = Vec::new();
    for path in paths {
        let output = checked(root, ["ls-tree", "-z", "HEAD", "--", path])?;
        if output.stdout.is_empty() {
            absent.push(path.clone());
        } else {
            present.push(path.clone());
        }
    }
    if !present.is_empty() {
        let mut restore = Command::new("git");
        restore
            .current_dir(root)
            .args(["restore", "--source=HEAD", "--staged", "--worktree", "--"])
            .args(&present);
        let output = restore.output()?;
        if !output.status.success() {
            bail!(
                "rollback restore failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }
    if !absent.is_empty() {
        let mut reset = Command::new("git");
        reset
            .current_dir(root)
            .args(["reset", "-q", "HEAD", "--"])
            .args(&absent);
        let output = reset.output()?;
        if !output.status.success() {
            bail!(
                "rollback reset failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        for path in absent {
            let target = root.join(path);
            if fs::symlink_metadata(&target).is_ok_and(|metadata| !metadata.is_dir()) {
                fs::remove_file(target)?;
            }
        }
    }
    Ok(())
}

pub fn push(root: &Path, config: &Config) -> Result<PushState> {
    if !config.git.push {
        return Ok(PushState::Disabled);
    }
    let mut command = Command::new("git");
    command.current_dir(root).arg("push");
    if let Some(remote) = &config.git.remote {
        command.arg("--set-upstream").arg(remote).arg("HEAD");
    }
    let output = command.output().context("push mdstore commits")?;
    if output.status.success() {
        return Ok(PushState::Pushed);
    }
    Ok(push_failure_state(&String::from_utf8_lossy(&output.stderr)))
}

fn push_failure_state(stderr: &str) -> PushState {
    let stderr = stderr.to_lowercase();
    if stderr.contains("non-fast-forward") || stderr.contains("fetch first") {
        PushState::Diverged
    } else {
        PushState::Queued
    }
}

pub fn has_unpushed(root: &Path) -> Result<bool> {
    if !run(root, ["rev-parse", "--verify", "@{upstream}"])?
        .status
        .success()
    {
        return Ok(true);
    }
    let output = checked(root, ["rev-list", "--count", "@{upstream}..HEAD"])?;
    Ok(String::from_utf8_lossy(&output.stdout).trim() != "0")
}

fn run<I, S>(root: &Path, args: I) -> Result<Output>
where
    I: IntoIterator<Item = S>,
    S: AsRef<std::ffi::OsStr>,
{
    Command::new("git")
        .current_dir(root)
        .args(args)
        .output()
        .context("run git")
}

fn checked<I, S>(root: &Path, args: I) -> Result<Output>
where
    I: IntoIterator<Item = S>,
    S: AsRef<std::ffi::OsStr>,
{
    let output = run(root, args)?;
    if !output.status.success() {
        bail!(
            "git command failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    Ok(output)
}

#[cfg(all(test, unix))]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use super::*;

    #[test]
    fn commit_tree_preserves_tree_and_message_without_running_hooks() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        checked(root, ["init", "-b", "main"]).unwrap();
        checked(root, ["config", "user.name", "mdstore test"]).unwrap();
        checked(root, ["config", "user.email", "mdstore@example.invalid"]).unwrap();
        checked(root, ["config", "commit.gpgsign", "false"]).unwrap();
        fs::write(root.join("note.md"), "old\n").unwrap();
        checked(root, ["add", "note.md"]).unwrap();
        checked(root, ["commit", "-m", "initial"]).unwrap();
        let parent = head(root).unwrap();

        fs::write(root.join("note.md"), "new\n").unwrap();
        let tree = stage_tree(root, &["note.md".into()]).unwrap().unwrap();
        let hook = git_dir(root).unwrap().join("hooks/commit-msg");
        fs::write(&hook, "#!/bin/sh\nexit 1\n").unwrap();
        let mut permissions = fs::metadata(&hook).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&hook, permissions).unwrap();

        let message = " exact summary \n\nsecond line";
        let commit = commit_tree(root, &tree, &parent, message).unwrap();
        assert_eq!(head(root).unwrap(), commit);
        assert_eq!(
            String::from_utf8(
                checked(root, ["show", "-s", "--format=%T", &commit])
                    .unwrap()
                    .stdout
            )
            .unwrap()
            .trim(),
            tree
        );
        assert_eq!(
            String::from_utf8(
                checked(root, ["show", "-s", "--format=%P", &commit])
                    .unwrap()
                    .stdout
            )
            .unwrap()
            .trim(),
            parent
        );
        let object = String::from_utf8(
            checked(root, ["cat-file", "commit", &commit])
                .unwrap()
                .stdout,
        )
        .unwrap();
        assert_eq!(object.split_once("\n\n").unwrap().1, message);
    }

    #[test]
    fn only_history_rejections_are_divergence() {
        assert_eq!(
            push_failure_state("! [rejected] main -> main (non-fast-forward)"),
            PushState::Diverged
        );
        assert_eq!(
            push_failure_state("! [remote rejected] main -> main (protected branch hook declined)"),
            PushState::Queued
        );
    }

    #[test]
    fn configured_push_establishes_upstream_and_tracks_unpushed_commits() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().join("repository");
        fs::create_dir(&root).unwrap();
        checked(directory.path(), ["init", "--bare", "remote.git"]).unwrap();
        checked(&root, ["init", "-b", "main"]).unwrap();
        checked(&root, ["config", "user.name", "mdstore test"]).unwrap();
        checked(&root, ["config", "user.email", "mdstore@example.invalid"]).unwrap();
        checked(&root, ["config", "commit.gpgsign", "false"]).unwrap();
        checked(&root, ["remote", "add", "origin", "../remote.git"]).unwrap();
        fs::write(root.join("note.md"), "one\n").unwrap();
        checked(&root, ["add", "note.md"]).unwrap();
        checked(&root, ["commit", "-m", "initial"]).unwrap();
        assert!(has_unpushed(&root).unwrap());

        let config = Config::from_yaml(
            "documents:\n  include: ['**/*.md']\nprovider:\n  dimensions: 2\ngit:\n  push: true\n  remote: origin\n",
        )
        .unwrap();
        assert_eq!(push(&root, &config).unwrap(), PushState::Pushed);
        let upstream = checked(&root, ["rev-parse", "--abbrev-ref", "@{upstream}"]).unwrap();
        assert_eq!(
            String::from_utf8(upstream.stdout).unwrap().trim(),
            "origin/main"
        );
        assert!(!has_unpushed(&root).unwrap());

        fs::write(root.join("note.md"), "two\n").unwrap();
        checked(&root, ["commit", "-am", "second"]).unwrap();
        assert!(has_unpushed(&root).unwrap());
        assert_eq!(push(&root, &config).unwrap(), PushState::Pushed);
        assert!(!has_unpushed(&root).unwrap());
    }

    #[test]
    fn ignored_paths_are_checked_in_one_batch_and_must_be_untracked() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        checked(root, ["init", "-b", "main"]).unwrap();
        fs::write(root.join(".gitignore"), "*.mdstore\n").unwrap();
        checked(root, ["add", ".gitignore"]).unwrap();
        checked(
            root,
            [
                "-c",
                "user.name=mdstore test",
                "-c",
                "user.email=mdstore@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                "ignore sidecars",
            ],
        )
        .unwrap();
        fs::write(root.join("one.mdstore"), "derived").unwrap();
        fs::write(root.join("two.mdstore"), "derived").unwrap();
        ensure_ignored(root, ["one.mdstore", "two.mdstore"]).unwrap();
        let many: Vec<String> = (0..2_000)
            .map(|index| format!("derived/{index:04}-{}.mdstore", "x".repeat(80)))
            .collect();
        ensure_ignored(root, many.iter().map(String::as_str)).unwrap();
        assert!(ensure_ignored(root, ["one.mdstore", "not-ignored.bin"]).is_err());

        checked(root, ["add", "-f", "one.mdstore"]).unwrap();
        assert!(ensure_ignored(root, ["one.mdstore"]).is_err());
    }

    #[test]
    fn repeated_quarantines_never_replace_an_earlier_copy() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        checked(root, ["init", "-b", "main"]).unwrap();
        checked(
            root,
            [
                "-c",
                "user.name=mdstore test",
                "-c",
                "user.email=mdstore@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-m",
                "initial",
            ],
        )
        .unwrap();
        fs::write(root.join("note.md"), "first").unwrap();
        recover_worktree(root).unwrap();
        fs::write(root.join("note.md"), "second").unwrap();
        recover_worktree(root).unwrap();

        let quarantine = git_dir(root).unwrap().join("mdstore/quarantine");
        let mut copies: Vec<String> = fs::read_dir(quarantine)
            .unwrap()
            .map(|entry| fs::read_to_string(entry.unwrap().path().join("note.md")).unwrap())
            .collect();
        copies.sort();
        assert_eq!(copies, ["first", "second"]);
    }

    #[test]
    fn commit_tree_never_replaces_an_advanced_head() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        checked(root, ["init", "-b", "main"]).unwrap();
        checked(root, ["config", "user.name", "mdstore test"]).unwrap();
        checked(root, ["config", "user.email", "mdstore@example.invalid"]).unwrap();
        checked(root, ["config", "commit.gpgsign", "false"]).unwrap();
        fs::write(root.join("note.md"), "old\n").unwrap();
        checked(root, ["add", "note.md"]).unwrap();
        checked(root, ["commit", "-m", "initial"]).unwrap();
        let parent = head(root).unwrap();
        fs::write(root.join("note.md"), "new\n").unwrap();
        let tree = stage_tree(root, &["note.md".into()]).unwrap().unwrap();
        checked(root, ["commit", "-m", "external commit"]).unwrap();
        let advanced = head(root).unwrap();

        assert!(commit_tree(root, &tree, &parent, "stale edit").is_err());
        assert_eq!(head(root).unwrap(), advanced);
    }

    #[test]
    fn rollback_after_lost_cas_restores_the_winning_head() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        checked(root, ["init", "-b", "main"]).unwrap();
        checked(root, ["config", "user.name", "mdstore test"]).unwrap();
        checked(root, ["config", "user.email", "mdstore@example.invalid"]).unwrap();
        checked(root, ["config", "commit.gpgsign", "false"]).unwrap();
        fs::write(root.join("existing.md"), "original\n").unwrap();
        checked(root, ["add", "existing.md"]).unwrap();
        checked(root, ["commit", "-m", "initial"]).unwrap();
        let parent = head(root).unwrap();

        fs::write(root.join("existing.md"), "daemon edit\n").unwrap();
        fs::write(root.join("new.md"), "daemon new\n").unwrap();
        let paths = vec!["existing.md".into(), "new.md".into()];
        let tree = stage_tree(root, &paths).unwrap().unwrap();
        checked(
            root,
            [
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--",
                "existing.md",
                "new.md",
            ],
        )
        .unwrap();

        fs::remove_file(root.join("existing.md")).unwrap();
        fs::write(root.join("new.md"), "external winner\n").unwrap();
        checked(root, ["add", "-A", "--", "existing.md", "new.md"]).unwrap();
        checked(root, ["commit", "-m", "external winner"]).unwrap();
        let winner = head(root).unwrap();

        fs::write(root.join("existing.md"), "daemon edit\n").unwrap();
        fs::write(root.join("new.md"), "daemon new\n").unwrap();
        checked(root, ["add", "-A", "--", "existing.md", "new.md"]).unwrap();
        assert!(commit_tree(root, &tree, &parent, "losing edit").is_err());
        rollback(root, &paths).unwrap();

        assert_eq!(head(root).unwrap(), winner);
        assert!(!root.join("existing.md").exists());
        assert_eq!(
            fs::read_to_string(root.join("new.md")).unwrap(),
            "external winner\n"
        );
        assert!(
            checked(root, ["status", "--porcelain"])
                .unwrap()
                .stdout
                .is_empty()
        );
    }
}
