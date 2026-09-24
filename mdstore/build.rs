//! Embeds the prebuilt SvelteKit SPA in the daemon.
use std::{
    env, fs,
    path::{Path, PathBuf},
};

fn collect(root: &Path, dir: &Path, output: &mut Vec<(String, String)>) {
    for entry in fs::read_dir(dir).expect("read built web assets") {
        let path = entry.expect("web asset entry").path();
        if path.is_dir() {
            collect(root, &path, output);
        } else {
            let relative = path
                .strip_prefix(root)
                .unwrap()
                .to_str()
                .unwrap()
                .replace('\\', "/");
            output.push((
                relative,
                path.canonicalize().unwrap().to_str().unwrap().to_owned(),
            ));
        }
    }
}

fn main() {
    if env::var_os("CARGO_FEATURE_SERVER").is_none() {
        return;
    }
    println!("cargo:rerun-if-env-changed=MDSTORE_WEB_DIST");
    let root = env::var_os("MDSTORE_WEB_DIST")
        .map(PathBuf::from)
        .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join("../webui/build"));
    println!("cargo:rerun-if-changed={}", root.display());
    assert!(
        root.join("index.html").is_file(),
        "Build the SvelteKit SPA first: cd webui && pnpm install --frozen-lockfile && pnpm build"
    );
    let mut assets = Vec::new();
    collect(&root, &root, &mut assets);
    assets.sort();
    let mut code = String::from("const WEB_ASSETS: &[(&str, &str, &[u8])] = &[\n");
    for (name, path) in assets {
        let mime = match Path::new(&name).extension().and_then(|s| s.to_str()) {
            Some("html") => "text/html; charset=utf-8",
            Some("woff2") => "font/woff2",
            Some("wasm") => "application/wasm",
            Some("js") => "text/javascript; charset=utf-8",
            Some("css") => "text/css; charset=utf-8",
            Some("json") => "application/json",
            Some("svg") => "image/svg+xml",
            _ => "application/octet-stream",
        };
        let route = if name == "index.html" {
            "/".to_owned()
        } else {
            format!("/{name}")
        };
        code.push_str(&format!(
            "({route:?}, {mime:?}, include_bytes!({path:?})),\n"
        ));
    }
    code.push_str("];\n");
    fs::write(
        Path::new(&env::var("OUT_DIR").unwrap()).join("web_assets.rs"),
        code,
    )
    .unwrap();
}
