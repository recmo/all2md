//! In-memory rumdl configuration and linting shared by server and browser.
use crate::markdown::Finding;
use anyhow::{Context, Result, bail};
use rumdl_lib::{
    config::{
        Config, ConfigSource, SourcedConfig, SourcedRuleConfig, SourcedValue, default_registry,
    },
    rule::Rule,
};
use std::{
    collections::HashMap,
    path::{Component, Path},
};

pub(crate) struct MarkdownLint {
    config: Config,
    rules: Vec<Box<dyn Rule>>,
    directory: std::path::PathBuf,
    source: (String, String),
}
impl MarkdownLint {
    pub(crate) fn source(&self) -> &(String, String) {
        &self.source
    }

    pub(crate) fn compile(
        template: &str,
        reference: &str,
        files: &HashMap<String, String>,
    ) -> Result<Self> {
        // A leading slash names the repository root, never the host filesystem.
        let (mut path, reference) = if let Some(reference) = reference.strip_prefix('/') {
            (std::path::PathBuf::new(), reference)
        } else {
            (
                Path::new(template)
                    .parent()
                    .unwrap_or(Path::new(""))
                    .to_path_buf(),
                reference,
            )
        };
        for component in Path::new(reference).components() {
            match component {
                Component::Normal(part) => path.push(part),
                Component::CurDir => {}
                Component::ParentDir => {
                    if !path.pop() {
                        bail!("rumdl config path escapes the repository");
                    }
                }
                _ => bail!("invalid rumdl config path"),
            }
        }
        let path = path.to_string_lossy().into_owned();
        crate::config::validate_repo_path(&path)?;
        if !crate::config::is_lint_config(&path) {
            bail!("reference a rumdl.toml or .rumdl.toml file");
        }
        let text = files
            .get(&path)
            .with_context(|| format!("missing rumdl configuration: {path}"))?;
        let mut config: Config = toml::from_str(text).with_context(|| format!("invalid {path}"))?;
        let raw: toml::Value = toml::from_str(text)?;
        if let Some(global) = raw.get("global").and_then(toml::Value::as_table) {
            for key in global.keys() {
                if !rumdl_lib::config::global_keys::is_global_value_key(&key.replace('_', "-")) {
                    bail!("{path}: unknown global option {key}");
                }
            }
        }
        if config.extends.is_some() {
            bail!(
                "{path}: extends is not supported in snapshot validation; reference a complete rumdl config"
            );
        }
        if config.global.editorconfig {
            bail!(
                "{path}: editorconfig requires a filesystem and is not supported in snapshot validation"
            );
        }
        if config.code_block_tools.enabled {
            bail!("{path}: external code-block tools are not supported in snapshot validation");
        }
        // Use upstream rule schemas; keep rumdl's full config rather than translating
        // a selection of options into our own configuration model.
        let registry = default_registry();
        let mut sourced = SourcedConfig::default();
        for (name, rule) in &config.rules {
            sourced.rules.insert(
                name.clone(),
                SourcedRuleConfig {
                    values: rule
                        .values
                        .iter()
                        .map(|(key, value)| {
                            (
                                key.clone(),
                                SourcedValue::new(value.clone(), ConfigSource::ProjectConfig),
                            )
                        })
                        .collect(),
                    ..Default::default()
                },
            );
        }
        let (_, warnings) = sourced.validate_into(&registry)?;
        if !warnings.is_empty() {
            bail!(
                "{path}: {}",
                warnings
                    .iter()
                    .map(|w| w.message.as_str())
                    .collect::<Vec<_>>()
                    .join("; ")
            );
        }
        config.global.enable_is_explicit =
            raw.get("global").and_then(|g| g.get("enable")).is_some();
        config.canonicalize_rule_lists();
        config.apply_per_rule_enabled();
        for name in config
            .global
            .enable
            .iter()
            .chain(&config.global.disable)
            .chain(&config.global.extend_enable)
            .chain(&config.global.extend_disable)
        {
            if !registry.rule_names().contains(name) {
                bail!("{path}: unknown rule {name}");
            }
        }
        if config
            .global
            .enable
            .iter()
            .chain(&config.global.extend_enable)
            .any(|name| name == "MD057")
        {
            bail!(
                "{path}: MD057 requires a filesystem; mdstore checks links against the repository snapshot"
            );
        }
        let rules =
            rumdl_lib::rules::filter_rules(&rumdl_lib::rules::all_rules(&config), &config.global)
                .into_iter()
                .filter(|r| r.name() != "MD057")
                .collect();
        Ok(Self {
            config,
            rules,
            directory: Path::new(&path).parent().unwrap().to_owned(),
            source: (path, text.clone()),
        })
    }
    pub(crate) fn validate(&self, path: &str, text: &str, findings: &mut Vec<Finding>) {
        let relative = Path::new(path)
            .strip_prefix(&self.directory)
            .unwrap_or(Path::new(path));
        if self.rules.is_empty()
            || rumdl_lib::discovery::ExcludeMatchers::new(&self.config.global.exclude)
                .is_match(&relative.to_string_lossy())
        {
            return;
        }
        let rules = rumdl_lib::rules::filter_rules_for_file(&self.rules, &self.config, relative);
        // No host filesystem path: both builds lint the supplied document only.
        match rumdl_lib::lint(
            text,
            &rules,
            false,
            self.config.get_flavor_for_file(relative),
            None,
            Some(&self.config),
        ) {
            Ok(warnings) => findings.extend(warnings.into_iter().map(|warning| Finding { source: None,
                path: path.into(),
                line: Some(warning.line),
                message: format!(
                    "{}: {}",
                    warning.rule_name.as_deref().unwrap_or("markdown"),
                    warning.message
                ),
            })),
            Err(error) => findings.push(Finding { source: None,
                path: path.into(),
                line: None,
                message: format!("Markdown lint failed: {error}"),
            }),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{markdown::validate_corpus, template::Templates};
    fn files(config: &str) -> HashMap<String, String> {
        HashMap::from([
            (
                "notes/schema.md".into(),
                "```starlark\nmarkdown('../rumdl.toml')\n```\n".into(),
            ),
            ("rumdl.toml".into(), config.into()),
        ])
    }
    #[test]
    fn native_toml_options_and_source_lines() {
        let templates = Templates::compile(&files(
            "[global]\nenable = ['MD012']\n[MD012]\nmaximum = 1\n",
        ))
        .unwrap();
        let docs = HashMap::from([(
            "notes/page.md".into(),
            "---\ntitle: Test\n---\nText\n\n\nMore\n".into(),
        )]);
        let findings = validate_corpus(&docs, &templates).unwrap_err();
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].line, Some(6));
        assert!(findings[0].message.starts_with("MD012:"));
        let relaxed = Templates::compile(&files(
            "[global]\nenable = ['MD012']\n[MD012]\nmaximum = 2\n",
        ))
        .unwrap();
        assert!(validate_corpus(&docs, &relaxed).is_ok());
        assert!(!templates.same_policy(&relaxed, "notes/page.md"));
        assert!(templates.same_policy(&relaxed, "other/page.md"));
    }
    #[test]
    fn root_relative_reference_uses_repository_config() {
        let mut configs = files("[global]\nenable=['MD012']\n");
        configs.insert("notes/rumdl.toml".into(), "[global]\nenable=[]\n".into());
        let root = MarkdownLint::compile("notes/schema.md", "/rumdl.toml", &configs).unwrap();
        let relative = MarkdownLint::compile("notes/schema.md", "rumdl.toml", &configs).unwrap();
        let mut findings = Vec::new();
        root.validate("notes/page.md", "A\n\n\nB\n", &mut findings);
        assert_eq!(findings.len(), 1);
        assert!(findings[0].message.starts_with("MD012:"));
        findings.clear();
        relative.validate("notes/page.md", "A\n\n\nB\n", &mut findings);
        assert!(findings.is_empty());
    }

    #[test]
    fn reject_missing_escaping_and_invalid_config() {
        for config in [
            "not toml",
            "[MD999]",
            "[MD012]\nmaximum='bad'",
            "[global]\nenable=['MD999']",
            "[MD012]\ntypo=1",
        ] {
            assert!(Templates::compile(&files(config)).is_err(), "{config}");
        }
        for reference in ["../../rumdl.toml", "/../rumdl.toml", "missing/rumdl.toml"] {
            let mut files = files("");
            files.insert(
                "notes/schema.md".into(),
                format!("```starlark\nmarkdown({reference:?})\n```\n"),
            );
            assert!(Templates::compile(&files).is_err(), "{reference}");
        }
    }
    #[test]
    fn respects_per_file_ignores_and_excludes() {
        let templates = Templates::compile(&files("[global]\nenable=['MD012']\nexclude=['notes/excluded.md']\n[per-file-ignores]\n'notes/ignored.md'=['MD012']\n")).unwrap();
        let docs = HashMap::from([
            ("notes/ignored.md".into(), "A\n\n\nB\n".into()),
            ("notes/excluded.md".into(), "A\n\n\nB\n".into()),
        ]);
        assert!(validate_corpus(&docs, &templates).is_ok());
    }
}
