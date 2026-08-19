use std::{
    collections::BTreeSet,
    fs,
    io::{Read, Seek, Write},
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};

use anyhow::{Context, Result, bail};
use serde::Serialize;

use crate::config::{
    Config, ensure_repository_path_safe, is_config_resource_path, validate_repo_path,
};

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

pub fn tracked_markdown(root: &Path, revision: &str, config: &Config) -> Result<Vec<String>> {
    let output = checked(root, ["ls-tree", "-r", "--name-only", "-z", revision])?;
    let (include, exclude) = config.document_globs()?;
    Ok(output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8_lossy(part).into_owned())
        .filter(|path| path.ends_with(".md"))
        .filter(|path| include.is_match(path) && !exclude.is_match(path))
        .collect())
}

pub fn tracked_config_files(root: &Path, revision: &str) -> Result<Vec<String>> {
    let output = checked(root, ["ls-tree", "-r", "--name-only", "-z", revision])?;
    Ok(output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8_lossy(part).into_owned())
        .filter(|path| is_config_resource_path(path))
        .collect())
}

pub fn untracked_sidecars(root: &Path) -> Result<Vec<String>> {
    let output = checked(
        root,
        [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        ],
    )?;
    Ok(output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8_lossy(part).into_owned())
        .filter(|path| path.ends_with(".mdstore"))
        .collect())
}

pub fn is_tracked(root: &Path, path: &str) -> Result<bool> {
    validate_repo_path(path)?;
    let pathspec = literal_pathspec(path);
    Ok(run(root, ["ls-files", "--error-unmatch", "--", &pathspec])?
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

pub fn read_text(root: &Path, revision: &str, path: &str) -> Result<String> {
    validate_repo_path(path)?;
    let pathspec = literal_pathspec(path);
    let entry = checked(root, ["ls-tree", "-z", revision, "--", &pathspec])?;
    if entry.stdout.starts_with(b"120000 ") {
        bail!("repository path may not traverse a symlink: {path}");
    }
    if !entry.stdout.starts_with(b"100644 ") && !entry.stdout.starts_with(b"100755 ") {
        bail!("repository path is not a regular file: {path}");
    }
    let object = format!("{revision}:{path}");
    let output = checked(root, ["show", &object])?;
    String::from_utf8(output.stdout).context("committed repository file is not UTF-8")
}

pub fn read_head_text(root: &Path, path: &str) -> Result<String> {
    read_text(root, "HEAD", path)
}

pub fn recover_worktree(root: &Path) -> Result<Vec<String>> {
    let worktree = checked(root, ["diff", "--name-only", "-z"])?;
    let staged = checked(
        root,
        [
            "diff",
            "--cached",
            "--no-renames",
            "--name-only",
            "-z",
            "HEAD",
        ],
    )?;
    let modified: Vec<String> = worktree
        .stdout
        .split(|byte| *byte == 0)
        .chain(staged.stdout.split(|byte| *byte == 0))
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8_lossy(part).into_owned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    quarantine_untracked_markdown(root)?;
    let mut staged_markdown = Vec::new();
    let mut present = Vec::new();
    let mut absent = Vec::new();
    for path in &modified {
        let pathspec = literal_pathspec(path);
        if checked(root, ["ls-tree", "-z", "HEAD", "--", &pathspec])?
            .stdout
            .is_empty()
        {
            absent.push(path.clone());
            if path.ends_with(".md") && root.join(path).exists() {
                staged_markdown.push(path.clone());
            }
        } else {
            present.push(path.clone());
        }
    }
    quarantine_paths(root, &staged_markdown)?;
    if !modified.is_empty() {
        let pathspecs: Vec<String> = modified.iter().map(|path| literal_pathspec(path)).collect();
        let mut reset = Command::new("git");
        reset
            .current_dir(root)
            .args(["reset", "-q", "HEAD", "--"])
            .args(&pathspecs);
        let output = reset.output().context("reset daemon-owned index entries")?;
        if !output.status.success() {
            bail!(
                "git reset failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }
    if !present.is_empty() {
        let pathspecs: Vec<String> = present.iter().map(|path| literal_pathspec(path)).collect();
        let mut restore = Command::new("git");
        restore
            .current_dir(root)
            .args(["restore", "--source=HEAD", "--worktree", "--"])
            .args(&pathspecs);
        let output = restore
            .output()
            .context("restore daemon-owned tracked files")?;
        if !output.status.success() {
            bail!(
                "git restore failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }
    for path in absent {
        let target = root.join(path);
        if fs::symlink_metadata(&target).is_ok_and(|metadata| !metadata.is_dir()) {
            fs::remove_file(target)?;
        }
    }
    Ok(modified)
}

fn quarantine_untracked_markdown(root: &Path) -> Result<()> {
    let output = checked(root, ["ls-files", "--others", "-z"])?;
    let paths: Vec<String> = output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8_lossy(part).into_owned())
        .filter(|path| path.ends_with(".md"))
        .collect();
    if paths.is_empty() {
        return Ok(());
    }
    quarantine_paths(root, &paths)
}

fn quarantine_paths(root: &Path, paths: &[String]) -> Result<()> {
    if paths.is_empty() {
        return Ok(());
    }
    let quarantine = create_quarantine_directory(root)?;
    for path in paths {
        validate_repo_path(path)?;
        let source = root.join(path);
        let target = quarantine.join(path);
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
            let permissions = fs::metadata(&target)
                .ok()
                .map(|metadata| metadata.permissions());
            fs::create_dir_all(parent)?;
            let mut temp = tempfile::NamedTempFile::new_in(parent)?;
            temp.write_all(content.as_bytes())?;
            temp.as_file().sync_all()?;
            if let Some(permissions) = permissions {
                temp.as_file().set_permissions(permissions)?;
            }
            temp.persist(&target).map_err(|error| error.error)?;
        } else if target.exists() {
            fs::remove_file(&target)?;
        }
    }
    Ok(())
}

pub fn stage_tree(
    root: &Path,
    base: &str,
    changes: &[(String, Option<String>)],
) -> Result<Option<String>> {
    let temporary = tempfile::tempdir_in(git_dir(root)?)?;
    let index = temporary.path().join("index");
    checked_with_index(root, &index, ["read-tree", base])?;
    let base_tree = checked(root, ["rev-parse", &format!("{base}^{{tree}}")])?;
    let base_tree = String::from_utf8(base_tree.stdout)?.trim().to_owned();
    for (path, content) in changes {
        validate_repo_path(path)?;
        match content {
            Some(content) => {
                let mut child = Command::new("git")
                    .current_dir(root)
                    .args(["hash-object", "-w", "--no-filters", "--stdin"])
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped())
                    .spawn()
                    .with_context(|| format!("store edited contents for {path}"))?;
                child
                    .stdin
                    .take()
                    .context("open git hash-object input")?
                    .write_all(content.as_bytes())?;
                let output = child.wait_with_output()?;
                if !output.status.success() {
                    bail!(
                        "git hash-object failed for {path}: {}",
                        String::from_utf8_lossy(&output.stderr)
                    );
                }
                let object = String::from_utf8(output.stdout)?.trim().to_owned();
                let pathspec = literal_pathspec(path);
                let existing = checked_with_index(
                    root,
                    &index,
                    ["ls-files", "--stage", "-z", "--", &pathspec],
                )?;
                let mode = existing
                    .stdout
                    .split(|byte| *byte == b' ')
                    .next()
                    .filter(|mode| !mode.is_empty())
                    .map(String::from_utf8_lossy)
                    .unwrap_or_else(|| "100644".into());
                let cacheinfo = format!("{mode},{object},{path}");
                let output = run_with_index(
                    root,
                    &index,
                    ["update-index", "--add", "--cacheinfo", &cacheinfo],
                )?;
                if !output.status.success() {
                    bail!(
                        "git update-index failed for {path}: {}",
                        String::from_utf8_lossy(&output.stderr)
                    );
                }
            }
            None => {
                let pathspec = literal_pathspec(path);
                let output = run_with_index(
                    root,
                    &index,
                    ["update-index", "--force-remove", "--", &pathspec],
                )?;
                if !output.status.success() {
                    bail!(
                        "git update-index failed for {path}: {}",
                        String::from_utf8_lossy(&output.stderr)
                    );
                }
            }
        }
    }
    let tree = checked_with_index(root, &index, ["write-tree"])?;
    let tree = String::from_utf8(tree.stdout)?.trim().to_owned();
    if tree == base_tree {
        return Ok(None);
    }
    Ok(Some(tree))
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

pub fn sync_index(root: &Path, revision: &str, paths: &[String]) -> Result<()> {
    let pathspecs: Vec<String> = paths.iter().map(|path| literal_pathspec(path)).collect();
    let mut command = Command::new("git");
    command
        .current_dir(root)
        .args(["reset", "-q", revision, "--"])
        .args(&pathspecs);
    let output = command
        .output()
        .context("synchronize committed index entries")?;
    if !output.status.success() {
        bail!(
            "git reset failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    Ok(())
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
        validate_repo_path(path)?;
        let pathspec = literal_pathspec(path);
        let output = checked(root, ["ls-tree", "-z", "HEAD", "--", &pathspec])?;
        if output.stdout.is_empty() {
            absent.push(path.clone());
        } else {
            present.push(path.clone());
        }
    }
    if !present.is_empty() {
        let pathspecs: Vec<String> = present.iter().map(|path| literal_pathspec(path)).collect();
        let mut restore = Command::new("git");
        restore
            .current_dir(root)
            .args(["restore", "--source=HEAD", "--staged", "--worktree", "--"])
            .args(&pathspecs);
        let output = restore.output()?;
        if !output.status.success() {
            bail!(
                "rollback restore failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }
    if !absent.is_empty() {
        let pathspecs: Vec<String> = absent.iter().map(|path| literal_pathspec(path)).collect();
        let mut reset = Command::new("git");
        reset
            .current_dir(root)
            .args(["reset", "-q", "HEAD", "--"])
            .args(&pathspecs);
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

pub fn push(root: &Path, config: &Config) -> PushState {
    if !config.git.push {
        return PushState::Disabled;
    }
    let mut command = Command::new("git");
    command.current_dir(root).arg("push");
    if let Some(remote) = &config.git.remote {
        command.arg("--set-upstream").arg(remote).arg("HEAD");
    }
    let Ok(Some(output)) = output_with_timeout(
        &mut command,
        Duration::from_secs(config.git.push_timeout_seconds),
    ) else {
        return PushState::Queued;
    };
    if output.status.success() {
        return PushState::Pushed;
    }
    push_failure_state(&String::from_utf8_lossy(&output.stderr))
}

fn output_with_timeout(command: &mut Command, timeout: Duration) -> Result<Option<Output>> {
    let mut stderr = tempfile::tempfile()?;
    command
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr.try_clone()?));
    let mut child = command.spawn()?;
    let started = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if started.elapsed() >= timeout {
            child.kill()?;
            child.wait()?;
            return Ok(None);
        }
        thread::sleep(Duration::from_millis(10));
    };
    stderr.rewind()?;
    let mut error = Vec::new();
    stderr.read_to_end(&mut error)?;
    Ok(Some(Output {
        status,
        stdout: Vec::new(),
        stderr: error,
    }))
}

fn push_failure_state(stderr: &str) -> PushState {
    let stderr = stderr.to_lowercase();
    if stderr.contains("non-fast-forward") || stderr.contains("fetch first") {
        PushState::Diverged
    } else {
        PushState::Queued
    }
}

fn literal_pathspec(path: &str) -> String {
    format!(":(literal){path}")
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

fn run_with_index<I, S>(root: &Path, index: &Path, args: I) -> Result<Output>
where
    I: IntoIterator<Item = S>,
    S: AsRef<std::ffi::OsStr>,
{
    Command::new("git")
        .current_dir(root)
        .env("GIT_INDEX_FILE", index)
        .args(args)
        .output()
        .context("run git with temporary index")
}

fn checked_with_index<I, S>(root: &Path, index: &Path, args: I) -> Result<Output>
where
    I: IntoIterator<Item = S>,
    S: AsRef<std::ffi::OsStr>,
{
    let output = run_with_index(root, index, args)?;
    if !output.status.success() {
        bail!(
            "git command failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    Ok(output)
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
        let tree = stage_tree(root, &parent, &[("note.md".into(), Some("new\n".into()))])
            .unwrap()
            .unwrap();
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
    fn recovery_repairs_index_after_ref_publication() {
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
        let tree = stage_tree(root, &parent, &[("note.md".into(), Some("new\n".into()))])
            .unwrap()
            .unwrap();
        commit_tree(root, &tree, &parent, "new content").unwrap();

        assert!(
            !checked(root, ["status", "--porcelain"])
                .unwrap()
                .stdout
                .is_empty()
        );
        recover_worktree(root).unwrap();
        assert!(
            checked(root, ["status", "--porcelain"])
                .unwrap()
                .stdout
                .is_empty()
        );
        assert_eq!(fs::read_to_string(root.join("note.md")).unwrap(), "new\n");
    }

    #[test]
    fn stage_tree_preserves_validated_bytes_despite_clean_filters_and_eol_rules() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        checked(root, ["init", "-b", "main"]).unwrap();
        checked(root, ["config", "user.name", "mdstore test"]).unwrap();
        checked(root, ["config", "user.email", "mdstore@example.invalid"]).unwrap();
        checked(root, ["config", "commit.gpgsign", "false"]).unwrap();
        fs::write(root.join("note.md"), "old\n").unwrap();
        checked(root, ["add", "note.md"]).unwrap();
        checked(root, ["commit", "-m", "initial"]).unwrap();
        fs::write(
            root.join(".gitattributes"),
            "*.md filter=uppercase text eol=crlf\n",
        )
        .unwrap();
        checked(root, ["config", "filter.uppercase.clean", "tr a-z A-Z"]).unwrap();
        checked(root, ["config", "filter.uppercase.smudge", "cat"]).unwrap();
        checked(root, ["config", "filter.uppercase.required", "true"]).unwrap();
        checked(root, ["add", ".gitattributes"]).unwrap();
        checked(root, ["commit", "-m", "attributes"]).unwrap();

        let validated = "exact lowercase bytes\n";
        fs::write(root.join("note.md"), "unvalidated concurrent bytes\n").unwrap();
        let tree = stage_tree(
            root,
            &head(root).unwrap(),
            &[("note.md".into(), Some(validated.into()))],
        )
        .unwrap()
        .unwrap();
        let committed = checked(root, ["show", &format!("{tree}:note.md")]).unwrap();
        assert_eq!(committed.stdout, validated.as_bytes());
    }

    #[test]
    fn exact_paths_with_pathspec_metacharacters_never_expand() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        checked(root, ["init", "-b", "main"]).unwrap();
        checked(root, ["config", "user.name", "mdstore test"]).unwrap();
        checked(root, ["config", "user.email", "mdstore@example.invalid"]).unwrap();
        checked(root, ["config", "commit.gpgsign", "false"]).unwrap();
        fs::write(root.join("note?.md"), "question old\n").unwrap();
        let mut permissions = fs::metadata(root.join("note?.md")).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(root.join("note?.md"), permissions).unwrap();
        fs::write(root.join("note1.md"), "one old\n").unwrap();
        checked(root, ["add", "note?.md", "note1.md"]).unwrap();
        checked(root, ["commit", "-m", "initial"]).unwrap();

        write_changes(root, &[("note?.md".into(), Some("question new\n".into()))]).unwrap();
        assert_ne!(
            fs::metadata(root.join("note?.md"))
                .unwrap()
                .permissions()
                .mode()
                & 0o111,
            0
        );
        fs::write(root.join("note1.md"), "one concurrent\n").unwrap();
        checked(root, ["add", "note1.md"]).unwrap();
        let tree = stage_tree(
            root,
            &head(root).unwrap(),
            &[("note?.md".into(), Some("question new\n".into()))],
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            checked(root, ["show", &format!("{tree}:note?.md")])
                .unwrap()
                .stdout,
            b"question new\n"
        );
        assert_eq!(
            checked(root, ["show", &format!("{tree}:note1.md")])
                .unwrap()
                .stdout,
            b"one old\n"
        );
        assert!(
            String::from_utf8_lossy(
                &checked(root, ["ls-tree", &tree, "--", ":(literal)note?.md"])
                    .unwrap()
                    .stdout
            )
            .starts_with("100755 ")
        );
        assert!(is_tracked(root, "note?.md").unwrap());
        assert!(!is_tracked(root, "missing?.md").unwrap());

        rollback(root, &["note?.md".into()]).unwrap();
        assert_eq!(
            fs::read_to_string(root.join("note?.md")).unwrap(),
            "question old\n"
        );
        assert_eq!(
            fs::read_to_string(root.join("note1.md")).unwrap(),
            "one concurrent\n"
        );
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
    fn command_timeout_terminates_a_stalled_push_process() {
        let mut command = Command::new("git");
        command
            .args(["hash-object", "--stdin"])
            .stdin(Stdio::piped());
        assert!(
            output_with_timeout(&mut command, Duration::from_millis(20))
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn command_timeout_accepts_the_largest_duration() {
        let mut command = Command::new("git");
        command.arg("--version");
        assert!(
            output_with_timeout(&mut command, Duration::from_secs(u64::MAX))
                .unwrap()
                .is_some()
        );
    }

    #[test]
    fn local_push_execution_failures_are_queued() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().to_owned();
        drop(directory);
        let config =
            Config::from_yaml("documents:\n  include: ['**/*.md']\nprovider:\n  dimensions: 2\n")
                .unwrap();
        assert_eq!(push(&root, &config), PushState::Queued);
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
        assert_eq!(push(&root, &config), PushState::Pushed);
        let upstream = checked(&root, ["rev-parse", "--abbrev-ref", "@{upstream}"]).unwrap();
        assert_eq!(
            String::from_utf8(upstream.stdout).unwrap().trim(),
            "origin/main"
        );
        assert!(!has_unpushed(&root).unwrap());

        fs::write(root.join("note.md"), "two\n").unwrap();
        checked(&root, ["commit", "-am", "second"]).unwrap();
        assert!(has_unpushed(&root).unwrap());
        assert_eq!(push(&root, &config), PushState::Pushed);
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
        fs::write(root.join(".gitignore"), "ignored.md\n").unwrap();
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
                "initial",
            ],
        )
        .unwrap();
        fs::write(root.join("note.md"), "first").unwrap();
        fs::write(root.join("ignored.md"), "ignored").unwrap();
        recover_worktree(root).unwrap();
        assert!(!root.join("ignored.md").exists());
        fs::write(root.join("note.md"), "second").unwrap();
        recover_worktree(root).unwrap();

        let quarantine = git_dir(root).unwrap().join("mdstore/quarantine");
        let directories: Vec<PathBuf> = fs::read_dir(quarantine)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .collect();
        let mut copies: Vec<String> = directories
            .iter()
            .map(|path| fs::read_to_string(path.join("note.md")).unwrap())
            .collect();
        copies.sort();
        assert_eq!(copies, ["first", "second"]);
        assert_eq!(
            directories
                .iter()
                .filter_map(|path| fs::read_to_string(path.join("ignored.md")).ok())
                .collect::<Vec<_>>(),
            ["ignored"]
        );
    }

    #[test]
    fn recovery_quarantines_staged_markdown_additions_and_rename_destinations() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        checked(root, ["init", "-b", "main"]).unwrap();
        checked(root, ["config", "user.name", "mdstore test"]).unwrap();
        checked(root, ["config", "user.email", "mdstore@example.invalid"]).unwrap();
        checked(root, ["config", "commit.gpgsign", "false"]).unwrap();
        fs::write(root.join("old.md"), "renamed bytes\n").unwrap();
        checked(root, ["add", "old.md"]).unwrap();
        checked(root, ["commit", "-m", "initial"]).unwrap();
        fs::write(root.join("added.md"), "added bytes\n").unwrap();
        checked(root, ["add", "added.md"]).unwrap();
        checked(root, ["mv", "old.md", "renamed.md"]).unwrap();

        recover_worktree(root).unwrap();

        assert_eq!(
            fs::read_to_string(root.join("old.md")).unwrap(),
            "renamed bytes\n"
        );
        assert!(!root.join("added.md").exists());
        assert!(!root.join("renamed.md").exists());
        let quarantine = git_dir(root).unwrap().join("mdstore/quarantine");
        let saved = fs::read_dir(quarantine)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .find(|path| path.join("added.md").exists())
            .unwrap();
        assert_eq!(
            fs::read_to_string(saved.join("added.md")).unwrap(),
            "added bytes\n"
        );
        assert_eq!(
            fs::read_to_string(saved.join("renamed.md")).unwrap(),
            "renamed bytes\n"
        );
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
        let tree = stage_tree(root, &parent, &[("note.md".into(), Some("new\n".into()))])
            .unwrap()
            .unwrap();
        checked(root, ["add", "note.md"]).unwrap();
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
        let changes = vec![
            ("existing.md".into(), Some("daemon edit\n".into())),
            ("new.md".into(), Some("daemon new\n".into())),
        ];
        let tree = stage_tree(root, &parent, &changes).unwrap().unwrap();
        rollback(root, &paths).unwrap();

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
