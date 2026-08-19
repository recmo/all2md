use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Output},
};

use anyhow::{Context, Result, bail};
use serde::Serialize;

use crate::config::{Config, validate_repo_path};

#[derive(Debug, Clone, Serialize)]
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
        ensure_safe_target(root, &target)?;
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

fn ensure_safe_target(root: &Path, target: &Path) -> Result<()> {
    let relative = target
        .strip_prefix(root)
        .context("target escapes repository")?;
    let mut current = root.to_path_buf();
    for component in relative.components() {
        current.push(component);
        if current.exists() && fs::symlink_metadata(&current)?.file_type().is_symlink() {
            bail!("refusing to write through symlink {}", current.display());
        }
    }
    Ok(())
}

pub fn commit(root: &Path, summary: &str, paths: &[String]) -> Result<bool> {
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
        return Ok(false);
    }
    let output = Command::new("git")
        .current_dir(root)
        .args(["commit", "-m", summary])
        .output()
        .context("commit mdstore edit batch")?;
    if !output.status.success() {
        bail!(
            "git commit failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    Ok(true)
}

pub fn rollback(root: &Path, paths: &[String], originally_present: &[String]) -> Result<()> {
    let mut unstage = Command::new("git");
    unstage
        .current_dir(root)
        .args(["restore", "--staged", "--"])
        .args(paths);
    let output = unstage.output()?;
    if !output.status.success() {
        bail!(
            "rollback unstage failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
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
    let stderr = String::from_utf8_lossy(&output.stderr).to_lowercase();
    if stderr.contains("non-fast-forward")
        || stderr.contains("fetch first")
        || stderr.contains("rejected")
    {
        Ok(PushState::Diverged)
    } else {
        Ok(PushState::Queued)
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
