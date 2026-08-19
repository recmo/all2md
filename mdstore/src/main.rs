use std::{net::SocketAddr, path::PathBuf};

use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use mdstore::{ApplyEditsRequest, Store};

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
    Serve {
        #[arg(long)]
        listen: Option<SocketAddr>,
    },
    Search {
        query: String,
        #[arg(long = "variant")]
        variants: Vec<String>,
    },
    Get {
        path: String,
        #[arg(long)]
        start_line: Option<usize>,
        #[arg(long)]
        end_line: Option<usize>,
    },
    Apply {
        #[arg(long)]
        file: PathBuf,
    },
    Validate,
    Reindex,
    Status,
    Push,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    let cli = Cli::parse();
    let store = Store::open(&cli.root)?;
    match cli.command {
        Command::Serve { listen } => {
            let config = store.config();
            let listen = listen.unwrap_or(
                config
                    .server
                    .listen
                    .parse()
                    .context("parse server.listen")?,
            );
            let token = config
                .server
                .bearer_token_env
                .as_deref()
                .map(std::env::var)
                .transpose()
                .context("read configured bearer token environment variable")?;
            let background = store.clone();
            tokio::spawn(async move {
                if let Err(error) = background.reindex().await {
                    tracing::warn!(%error, "background embedding rebuild is degraded");
                }
            });
            mdstore::mcp::serve(store, listen, token).await?;
        }
        Command::Search { query, variants } => {
            print_json(&store.search(&query, &variants).await?)?;
        }
        Command::Get {
            path,
            start_line,
            end_line,
        } => {
            if end_line.is_some() && start_line.is_none() {
                bail!("--end-line requires --start-line");
            }
            let window = start_line.map(|start| (start, end_line.unwrap_or(start)));
            print_json(&store.get_page(&path, window)?)?;
        }
        Command::Apply { file } => {
            let request: ApplyEditsRequest = serde_json::from_slice(&std::fs::read(file)?)?;
            let response = store.apply_edits(&request)?;
            if let Err(error) = store.reindex_after_changes(&response.touched_paths).await {
                tracing::warn!(%error, "post-edit embedding rebuild is degraded");
            }
            print_json(&response)?;
        }
        Command::Validate => match store.validate() {
            Ok(()) => println!("valid"),
            Err(findings) => {
                print_json(&findings)?;
                bail!("corpus validation failed");
            }
        },
        Command::Reindex => {
            store.reindex().await?;
            print_json(&store.status()?)?;
        }
        Command::Status => print_json(&store.status()?)?,
        Command::Push => print_json(&store.push()?)?,
    }
    Ok(())
}

fn print_json(value: &impl serde::Serialize) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
}
