use std::{
    net::SocketAddr,
    path::{Path, PathBuf},
    process::Command as ProcessCommand,
};

use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use mdstore::{ApplyEditsRequest, Config, Store};
use serde_json::{Value, json};

#[derive(Debug, Parser)]
#[command(version, about)]
struct Cli {
    #[arg(long, global = true, default_value = ".")]
    root: PathBuf,
    #[arg(long, global = true, env = "MDSTORE_URL")]
    daemon_url: Option<String>,
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    Serve,
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
    let root = repository_root(&cli.root)?;
    match cli.command {
        Command::Serve => {
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
            let background = store.clone();
            tokio::spawn(async move {
                if let Err(error) = background.reindex_missing().await {
                    tracing::warn!(%error, "background embedding rebuild is degraded");
                }
            });
            mdstore::mcp::serve(store, listen, token).await?;
        }
        Command::Search { query, variants } => {
            let client = DaemonClient::from_repository(&root, cli.daemon_url.as_deref())?;
            print_json(
                &client
                    .call_tool("search", json!({"query": query, "variants": variants}))
                    .await?,
            )?;
        }
        Command::Get {
            path,
            start_line,
            end_line,
        } => {
            if end_line.is_some() && start_line.is_none() {
                bail!("--end-line requires --start-line");
            }
            let client = DaemonClient::from_repository(&root, cli.daemon_url.as_deref())?;
            let mut arguments = json!({"path": path});
            if let Some(start) = start_line {
                arguments["start_line"] = json!(start);
                arguments["end_line"] = json!(end_line.unwrap_or(start));
            }
            print_json(&client.call_tool("get_page", arguments).await?)?;
        }
        Command::Apply { file } => {
            let request: ApplyEditsRequest = serde_json::from_slice(&std::fs::read(file)?)?;
            let client = DaemonClient::from_repository(&root, cli.daemon_url.as_deref())?;
            print_json(
                &client
                    .call_tool("apply_edits", serde_json::to_value(request)?)
                    .await?,
            )?;
        }
        Command::Validate => {
            let client = DaemonClient::from_repository(&root, cli.daemon_url.as_deref())?;
            let response = client.command("validate").await?;
            if response["valid"].as_bool() == Some(true) {
                println!("valid");
            } else {
                print_json(&response["findings"])?;
                bail!("corpus validation failed");
            }
        }
        Command::Reindex => {
            let client = DaemonClient::from_repository(&root, cli.daemon_url.as_deref())?;
            print_json(&client.command("reindex").await?)?;
        }
        Command::Status => {
            let client = DaemonClient::from_repository(&root, cli.daemon_url.as_deref())?;
            print_json(&client.status().await?)?;
        }
        Command::Push => {
            let client = DaemonClient::from_repository(&root, cli.daemon_url.as_deref())?;
            print_json(&client.command("push").await?)?;
        }
    }
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

struct DaemonClient {
    client: reqwest::Client,
    base_url: String,
    bearer_token: Option<String>,
}

impl DaemonClient {
    fn from_repository(root: &std::path::Path, override_url: Option<&str>) -> Result<Self> {
        let config =
            Config::from_yaml(&mdstore::git::read_head_text(root, ".mdstore/config.yaml")?)?;
        let base_url = match override_url {
            Some(url) if !url.trim().is_empty() => url.trim_end_matches('/').to_owned(),
            _ => {
                let mut address: SocketAddr = config
                    .server
                    .listen
                    .parse()
                    .context("parse server.listen")?;
                if address.ip().is_unspecified() {
                    address.set_ip(if address.is_ipv4() {
                        std::net::Ipv4Addr::LOCALHOST.into()
                    } else {
                        std::net::Ipv6Addr::LOCALHOST.into()
                    });
                }
                format!("http://{address}")
            }
        };
        let bearer_token = config
            .server
            .bearer_token_env
            .as_deref()
            .map(std::env::var)
            .transpose()
            .context("read configured bearer token environment variable")?;
        Ok(Self {
            client: reqwest::Client::new(),
            base_url,
            bearer_token,
        })
    }

    async fn call_tool(&self, name: &str, arguments: Value) -> Result<Value> {
        let response = self
            .post(
                "/mcp",
                json!({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}
                }),
            )
            .await?;
        if let Some(error) = response.get("error") {
            bail!("daemon RPC error: {error}");
        }
        let result = response
            .get("result")
            .context("daemon RPC response has no result")?;
        if result["isError"].as_bool() == Some(true) {
            let message = result["content"]
                .as_array()
                .and_then(|content| content.first())
                .and_then(|item| item["text"].as_str())
                .unwrap_or("daemon tool call failed");
            bail!("{message}");
        }
        result
            .get("structuredContent")
            .cloned()
            .context("daemon tool response has no structured content")
    }

    async fn command(&self, command: &str) -> Result<Value> {
        self.post("/cli", json!({"command": command})).await
    }

    async fn status(&self) -> Result<Value> {
        let response = self.send(self.client.get(self.url("/health"))).await?;
        response
            .get("store")
            .cloned()
            .context("daemon health response has no store status")
    }

    async fn post(&self, path: &str, body: Value) -> Result<Value> {
        self.send(self.client.post(self.url(path)).json(&body))
            .await
    }

    async fn send(&self, mut request: reqwest::RequestBuilder) -> Result<Value> {
        if let Some(token) = &self.bearer_token {
            request = request.bearer_auth(token);
        }
        let response = request.send().await.context("call mdstore daemon")?;
        let status = response.status();
        let body = response.bytes().await.context("read daemon response")?;
        if !status.is_success() {
            bail!(
                "mdstore daemon returned {status}: {}",
                String::from_utf8_lossy(&body)
            );
        }
        serde_json::from_slice(&body).context("decode daemon response")
    }

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.base_url)
    }
}

fn print_json(value: &impl serde::Serialize) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::{fs, process::Command};

    use super::*;

    fn git(root: &std::path::Path, arguments: &[&str]) {
        let output = Command::new("git")
            .current_dir(root)
            .args(arguments)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    #[test]
    fn daemon_discovery_ignores_uncommitted_config_and_credentials() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        git(root, &["init", "-b", "main"]);
        git(root, &["config", "user.name", "mdstore test"]);
        git(root, &["config", "user.email", "mdstore@example.invalid"]);
        git(root, &["config", "commit.gpgsign", "false"]);
        fs::create_dir(root.join(".mdstore")).unwrap();
        let committed = "documents:\n  include: ['**/*.md']\nprovider:\n  dimensions: 2\ngit:\n  push: false\nserver:\n  listen: 127.0.0.1:3131\n";
        fs::write(root.join(".mdstore/config.yaml"), committed).unwrap();
        git(root, &["add", ".mdstore/config.yaml"]);
        git(root, &["commit", "-m", "configuration"]);

        fs::write(
            root.join(".mdstore/config.yaml"),
            committed.replace(
                "  listen: 127.0.0.1:3131",
                "  listen: 203.0.113.1:4444\n  bearer_token_env: PATH",
            ),
        )
        .unwrap();
        let client = DaemonClient::from_repository(root, None).unwrap();
        assert_eq!(client.base_url, "http://127.0.0.1:3131");
        assert!(client.bearer_token.is_none());

        let client = DaemonClient::from_repository(root, Some("http://127.0.0.1:4141/")).unwrap();
        assert_eq!(client.base_url, "http://127.0.0.1:4141");
        assert!(client.bearer_token.is_none());
    }

    #[test]
    fn nested_root_resolves_to_the_repository_toplevel() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        git(root, &["init", "-b", "main"]);
        let nested = root.join("notes/archive");
        fs::create_dir_all(&nested).unwrap();

        assert_eq!(
            repository_root(&nested).unwrap(),
            root.canonicalize().unwrap()
        );
    }
}
