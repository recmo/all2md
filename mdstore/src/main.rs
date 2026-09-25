//! Mdstore daemon entry point.
use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use mdstore::{Store, serve};
use std::{
    path::{Path, PathBuf},
    process::Command as ProcessCommand,
};

#[derive(Debug, Parser)]
#[command(version, about)]
struct Cli {
    #[arg(long, global = true, default_value = ".")]
    root: PathBuf,
    #[command(subcommand)]
    command: Command,
}
#[derive(Debug, Subcommand)]
enum Command {
    Serve,
}
#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    let cli = Cli::parse();
    let root = repository_root(&cli.root)?;
    let store = Store::open(&root)?;
    let config = store.config();
    let listen = config
        .server
        .listen
        .parse()
        .context("parse server.listen")?;
    let token = config
        .server
        .bearer_token_env
        .as_deref()
        .map(std::env::var)
        .transpose()
        .context("read configured bearer token environment variable")?;
    serve(store, listen, token).await?;
    Ok(())
}

fn repository_root(path: &Path) -> Result<PathBuf> {
    let output = ProcessCommand::new("git")
        .current_dir(path)
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .with_context(|| format!("locate Git repository for {}", path.display()))?;
    if !output.status.success() {
        bail!(
            "locate Git repository for {}: {}",
            path.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let root = std::str::from_utf8(&output.stdout)
        .context("Git repository root is not UTF-8")?
        .trim();
    if root.is_empty() {
        bail!("Git returned an empty repository root");
    }
    std::fs::canonicalize(root).with_context(|| format!("canonicalize Git repository root {root}"))
}
