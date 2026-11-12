use anyhow::Context as _;
use genvm_common::sync;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::manager::{chainforks, Config};
use genvm_common::*;

#[derive(serde::Serialize, serde::Deserialize, Clone)]
pub struct ExecutorVersion {
    /// The line is selectable *iff* this fork is active.
    pub fork: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Version {
    pub major: u32,
    pub minor: u32,
    pub patch: u32,
}

impl Version {
    /// Reads a manifest key such as `v0.3.0-rc7`. The prerelease suffix is part
    /// of the key but not of the ordering, so it is dropped here.
    pub fn parse(s: &str) -> Result<Self, &'static str> {
        let parts: Vec<&str> = s.split('.').collect();
        if parts.len() != 3 {
            return Err("Invalid version format");
        }
        let part0 = parts[0].strip_prefix("v").unwrap_or(parts[0]);
        let patch_str = parts[2].split('-').next().unwrap_or(parts[2]);

        Ok(Version {
            major: part0.parse().map_err(|_| "Invalid major version")?,
            minor: parts[1].parse().map_err(|_| "Invalid minor version")?,
            patch: patch_str.parse().map_err(|_| "Invalid patch version")?,
        })
    }
}

impl<'de> serde::Deserialize<'de> for Version {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let s = String::deserialize(deserializer)?;

        Version::parse(&s).map_err(serde::de::Error::custom)
    }
}

#[derive(Clone)]
pub struct Manifest {
    pub executor_versions: std::collections::BTreeMap<Version, ExecutorVersion>,
    pub version_orig_keys: std::collections::BTreeMap<Version, String>,
    /// Every fork a runner names; the schedule must define each.
    pub runner_forks: Vec<String>,
}

impl<'de> serde::Deserialize<'de> for Manifest {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        #[derive(serde::Deserialize)]
        struct Raw {
            executor_versions: std::collections::BTreeMap<String, ExecutorVersion>,
            runner_forks: Vec<String>,
        }
        let raw = Raw::deserialize(deserializer)?;
        let mut executor_versions = std::collections::BTreeMap::new();
        let mut version_orig_keys = std::collections::BTreeMap::new();
        for (key, value) in raw.executor_versions {
            let ver: Version = serde::Deserialize::deserialize(
                serde::de::value::StringDeserializer::<D::Error>::new(key.clone()),
            )?;
            if let Some(prev_key) = version_orig_keys.get(&ver) {
                return Err(serde::de::Error::custom(format!(
                    "duplicate version {}.{}.{} in manifest ({} and {})",
                    ver.major, ver.minor, ver.patch, prev_key, key,
                )));
            }
            executor_versions.insert(ver, value);
            version_orig_keys.insert(ver, key);
        }
        Ok(Manifest {
            executor_versions,
            version_orig_keys,
            runner_forks: raw.runner_forks,
        })
    }
}

#[derive(Debug)]
pub struct ResolvedVersion {
    pub version: Version,
    pub orig_key: String,
}

pub struct Ctx {
    config: sync::DArc<Config>,
    chainforks: chainforks::Schedule,
    manifest: tokio::sync::RwLock<Manifest>,
}

/// The schedule and manifest the manager runs with, validated against each other.
pub fn load_install(config: &Config) -> anyhow::Result<(chainforks::Schedule, Manifest)> {
    let path = &config.chainforks_path;
    let content = std::fs::read_to_string(path)
        .with_context(|| format!("reading chainforks schedule {path}"))?;
    let chainforks =
        chainforks::Schedule::parse(&content).with_context(|| format!("loading {path}"))?;
    if let Some(network) = &config.default_network {
        chainforks
            .check_network(network)
            .with_context(|| "checking default_network")?;
    }
    let content = std::fs::read_to_string(&config.manifest_path)
        .with_context(|| format!("Failed to read manifest file: {}", config.manifest_path))?;
    let manifest = parse_manifest(&content, &config.manifest_path, &chainforks)?;
    Ok((chainforks, manifest))
}

async fn load_manifest(
    manifest_path: &str,
    chainforks: &chainforks::Schedule,
) -> anyhow::Result<Manifest> {
    let content = tokio::fs::read_to_string(manifest_path)
        .await
        .with_context(|| format!("Failed to read manifest file: {}", manifest_path))?;
    parse_manifest(&content, manifest_path, chainforks)
}

fn parse_manifest(
    content: &str,
    manifest_path: &str,
    chainforks: &chainforks::Schedule,
) -> anyhow::Result<Manifest> {
    let manifest: Manifest =
        serde_yaml::from_str(content).with_context(|| "Failed to parse manifest YAML")?;
    for (ver, ev) in &manifest.executor_versions {
        chainforks.check_defined(&ev.fork).with_context(|| {
            format!(
                "executor version {} in manifest {manifest_path}",
                manifest.version_orig_keys[ver]
            )
        })?;
    }
    for fork in &manifest.runner_forks {
        chainforks
            .check_defined(fork)
            .with_context(|| format!("runner fork in manifest {manifest_path}"))?;
    }
    Ok(manifest)
}

impl Ctx {
    pub async fn new(config: sync::DArc<Config>) -> anyhow::Result<Self> {
        let (chainforks, manifest) = load_install(&config)?;
        Ok(Self {
            manifest: tokio::sync::RwLock::new(manifest),
            chainforks,
            config,
        })
    }

    pub async fn reload_manifest(&self) -> anyhow::Result<()> {
        let manifest = load_manifest(&self.config.manifest_path, &self.chainforks).await?;

        let mut lock = self.manifest.write().await;
        *lock = manifest;

        Ok(())
    }

    /// The forks active on `network` at `timestamp`; an unknown network is an error.
    pub fn active_forks(
        &self,
        network: &str,
        timestamp: chrono::DateTime<chrono::Utc>,
    ) -> Result<chainforks::Active, chainforks::Error> {
        self.chainforks.active(network, timestamp)
    }

    pub async fn get_latest_major(&self, forks: &chainforks::Active) -> Option<u32> {
        let lock = self.manifest.read().await;
        lock.executor_versions
            .iter()
            .filter(|(_, ev)| forks.contains(&ev.fork))
            .map(|(ver, _)| ver.major)
            .max()
    }

    pub async fn get_version(
        &self,
        major: u32,
        forks: &chainforks::Active,
    ) -> Option<ResolvedVersion> {
        let lock = self.manifest.read().await;
        resolve_version(&lock, forks, |ver, _| ver.major == major)
    }

    /// Newest active line, whatever its major.
    ///
    /// The fallback for a major no installed line provides: the run still
    /// reaches an executor, which rejects the contract with a canonical
    /// `invalid_contract major_mismatch` instead of never starting.
    pub async fn get_newest_version(&self, forks: &chainforks::Active) -> Option<ResolvedVersion> {
        let lock = self.manifest.read().await;
        resolve_version(&lock, forks, |_, _| true)
    }

    /// Newest line whose manifest key matches `pattern`, by the same rules a
    /// major goes through.
    pub async fn get_matching_version(
        &self,
        pattern: &regex::Regex,
        forks: &chainforks::Active,
    ) -> Option<ResolvedVersion> {
        let lock = self.manifest.read().await;
        resolve_version(&lock, forks, |_, key| pattern.is_match(key))
    }
}

/// The line an exact version string names.
///
/// No manifest lookup and no rules: the string is the executor directory. The
/// version it reports back is read off the name for logging alone, and a name
/// that is not a version at all still runs -- an exact pin is a statement by a
/// party we trust, not a request to be resolved.
pub fn exact_version(version: &str) -> ResolvedVersion {
    ResolvedVersion {
        version: Version::parse(version).unwrap_or(Version {
            major: 0,
            minor: 0,
            patch: 0,
        }),
        orig_key: version.to_owned(),
    }
}

fn resolve_version(
    manifest: &Manifest,
    forks: &chainforks::Active,
    matches: impl Fn(&Version, &str) -> bool,
) -> Option<ResolvedVersion> {
    let matches = |ver: &Version| match manifest.version_orig_keys.get(ver) {
        Some(key) => matches(ver, key),
        None => false,
    };

    let ver = manifest
        .executor_versions
        .iter()
        .filter(|(ver, ev)| matches(ver) && forks.contains(&ev.fork))
        .map(|(ver, _)| *ver)
        .max()?;

    Some(ResolvedVersion {
        version: ver,
        orig_key: manifest.version_orig_keys.get(&ver)?.clone(),
    })
}

pub async fn detect_major_spec(
    full_ctx: &crate::manager::AppContext,
    data: &[u8],
    forks: &chainforks::Active,
) -> anyhow::Result<u32> {
    let zelf = &full_ctx.ver_ctx;

    let Some(possible_major) = zelf.get_latest_major(forks).await else {
        anyhow::bail!("no_executor_version_available");
    };

    let execute_in = zelf
        .get_version(possible_major, forks)
        .await
        .with_context(|| "failed_to_get_executor_version")?;

    let mut genvm_path = std::path::PathBuf::from(full_ctx.run_ctx.executors_path());

    genvm_path.push(&execute_in.orig_key);
    genvm_path.push("bin");
    genvm_path.push("genvm");

    let mut proc = tokio::process::Command::new(&genvm_path)
        .arg("parse-version-pattern")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .with_context(|| format!("running genvm command {:?}", genvm_path))?;

    let stdin = proc.stdin.take();
    let stdout = proc.stdout.take();

    let task = async move {
        let mut stdin = stdin.with_context(|| "failed_to_open_stdin")?;
        stdin
            .write_all(data)
            .await
            .with_context(|| "failed_to_write_to_stdin")?;
        stdin.flush().await?;
        std::mem::drop(stdin);
        let mut res_str = String::new();
        stdout
            .with_context(|| "failed_to_open_stdout")?
            .read_to_string(&mut res_str)
            .await?;

        log_debug!(version_string = res_str; "read version pattern");

        let res = res_str.trim();
        let res = res.strip_prefix("v").unwrap_or(res);
        let res = &res[..res.find('.').unwrap_or(res.len())];

        let res = res.parse::<u32>().unwrap_or(possible_major);

        log_debug!(version_string = res_str, version = res; "version pattern parsed");

        Ok(res)
    };

    let detected_version = task.await;

    let _ = proc.wait().await;

    detected_version.map(|v| v.min(possible_major))
}

#[cfg(test)]
#[path = "versioning_test.rs"]
mod tests;
