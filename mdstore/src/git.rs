use std::{
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

pub fn head(root: &Path) -> Result<String> {
    let output = checked(root, ["rev-parse", "HEAD"])?;
    Ok(String::from_utf8(output.stdout)?.trim().into())
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
    let quarantine = git_dir(root)?.join("mdstore/quarantine").join(format!(
        "{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_secs()
    ));
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

pub fn rollback(root: &Path, paths: &[String], originally_present: &[String]) -> Result<()> {
    let mut staged = Command::new("git");
    staged
        .current_dir(root)
        .args(["diff", "--cached", "--name-only", "-z", "--"])
        .args(paths);
    let output = staged.output()?;
    if !output.status.success() {
        bail!("inspect staged rollback paths failed");
    }
    let staged: Vec<String> = output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8_lossy(part).into_owned())
        .collect();
    if !staged.is_empty() {
        let mut unstage = Command::new("git");
        unstage
            .current_dir(root)
            .args(["restore", "--staged", "--"])
            .args(&staged);
        let output = unstage.output()?;
        if !output.status.success() {
            bail!(
                "rollback unstage failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }
    if !originally_present.is_empty() {
        let mut restore = Command::new("git");
        restore
            .current_dir(root)
            .args(["restore", "--worktree", "--"])
            .args(originally_present);
        let output = restore.output()?;
        if !output.status.success() {
            bail!(
                "rollback restore failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }
    for path in paths {
        if !originally_present.contains(path) {
            let target = root.join(path);
            if target.is_file() {
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
        command.arg(remote).arg("HEAD");
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
    let output = run(root, ["rev-list", "--count", "@{upstream}..HEAD"])?;
    if !output.status.success() {
        return Ok(false);
    }
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
    fn commit_tree_never_replaces_an_advanced_head() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        checked(root, ["init", "-b", "main"]).unwrap();
        checked(root, ["config", "user.name", "mdstore test"]).unwrap();
        checked(root, ["config", "user.email", "mdstore@example.invalid"]).unwrap();
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
}
