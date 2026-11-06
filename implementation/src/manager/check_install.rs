use std::collections::HashMap;

use anyhow::{Context as _, Result};
use genvm_common::*;

use super::versioning;
use super::{CliArgs, Config};

#[derive(clap::Args, Debug)]
pub struct Args {
    #[arg(
        long,
        default_value_t = false,
        help = "also precompile every runner into each executor's on-disk cache"
    )]
    pub precompile: bool,
}

/// Validates the schedule and manifest as startup does, then runs `genvm check`
/// for every executor listed in the manifest, verifying that its runners are
/// present with the correct hashes and name only forks the schedule defines
/// (and, with `--precompile`, precompiling them). Each executor resolves its
/// own runner registry and directory from its bundled config, so the manager
/// only has to locate and invoke the right binaries.
pub fn run(cli: &CliArgs, args: &Args) -> Result<()> {
    let config =
        genvm_common::load_config(HashMap::new(), &cli.config).with_context(|| "loading config")?;
    let mut config: Config = serde_yaml::from_value(config)?;

    if let Some(manifest_path) = &cli.manifest_path {
        config.manifest_path = manifest_path.clone();
    }

    config.base.setup_logging(std::io::stdout())?;

    let (chainforks, manifest) = versioning::load_install(&config)?;
    let defined_forks = chainforks
        .forks
        .keys()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join(",");

    // Executors live next to this binary: <root>/bin/genvm-modules -> <root>/executor
    let mut executors_path = std::env::current_exe()?;
    executors_path.pop();
    executors_path.pop();
    executors_path.push("executor");

    let mut failures: Vec<String> = Vec::new();

    for version in manifest.executor_versions.keys() {
        let orig_key = manifest
            .version_orig_keys
            .get(version)
            .with_context(|| format!("missing original manifest key for {version:?}"))?;

        let genvm = executors_path.join(orig_key).join("bin").join("genvm");

        log_info!(version = orig_key.as_str(), exe:? = genvm, precompile = args.precompile; "checking executor install");

        let mut command = std::process::Command::new(&genvm);
        command
            .arg("check")
            .arg("--defined-forks")
            .arg(&defined_forks);
        if args.precompile {
            command.arg("--precompile");
        }

        let status = command
            .status()
            .with_context(|| format!("spawning {genvm:?}"))?;

        if !status.success() {
            log_error!(@operator, version = orig_key.as_str(), status:? = status; "executor check failed");
            failures.push(orig_key.clone());
        }
    }

    if !failures.is_empty() {
        anyhow::bail!(
            "check-install failed for executors: {}",
            failures.join(", ")
        );
    }

    log_info!(@operator; "check-install succeeded for all executors");

    Ok(())
}
