use super::*;

const V02: &str = "genvm/executor/v0.2";
const V03: &str = "genvm/executor/v0.3";
const V03_PATCH: &str = "genvm/executor/v0.3/patch";

/// The forks of [`live_manifest`] and [`patch_manifest`], `active` on and the rest off.
fn forks(active: &[&str]) -> chainforks::Active {
    let date = |fork: &str| {
        if active.contains(&fork) {
            "always"
        } else {
            "never"
        }
    };
    let json = format!(
        r#"{{
            "forks": {{ "{V02}": {{}}, "{V03}": {{}}, "{V03_PATCH}": {{ "requires": ["{V03}"] }} }},
            "networks": {{ "net": {{ "forks": {{
                "{V02}": "{}", "{V03}": "{}", "{V03_PATCH}": "{}"
            }} }} }}
        }}"#,
        date(V02),
        date(V03),
        date(V03_PATCH),
    );
    chainforks::Schedule::parse(&json)
        .unwrap()
        .active("net", "2025-01-01T00:00:00Z".parse().unwrap())
        .unwrap()
}

fn all_forks() -> chainforks::Active {
    forks(&[V02, V03, V03_PATCH])
}

/// The manifest the manager ships: two live lines, both semver major zero.
/// `sha256` is post-install's pin map; the manager must tolerate it.
fn live_manifest() -> Manifest {
    serde_yaml::from_str(&format!(
        "runner_forks: []\nexecutor_versions:\n  \"v0.2.17\":\n    fork: {V02}\n  \"v0.3.0-rc7\":\n    fork: {V03}\n    sha256:\n      amd64-linux: \"0000000000000000000000000000000000000000000000000000000000000000\"\n",
    ))
    .unwrap()
}

/// 3 contiguous patches, the last behind its own fork.
fn patch_manifest() -> Manifest {
    serde_yaml::from_str(&format!(
        "runner_forks: []\nexecutor_versions:\n  \"v0.3.0\":\n    fork: {V03}\n  \"v0.3.1\":\n    fork: {V03}\n  \"v0.3.2\":\n    fork: {V03_PATCH}\n",
    ))
    .unwrap()
}

#[test]
fn a_major_cannot_separate_two_zero_major_lines() {
    let manifest = live_manifest();

    // Both keys are major 0, so the only major matching anything matches both
    // and the newest wins. This is why a version string exists.
    let resolved = resolve_version(&manifest, &all_forks(), |ver, _| ver.major == 0).unwrap();
    assert_eq!(resolved.orig_key, "v0.3.0-rc7");

    assert!(resolve_version(&manifest, &all_forks(), |ver, _| ver.major == 2).is_none());
}

#[test]
fn a_line_whose_fork_is_inactive_is_skipped() {
    let manifest = live_manifest();

    let resolved = resolve_version(&manifest, &forks(&[V02]), |ver, _| ver.major == 0).unwrap();
    assert_eq!(resolved.orig_key, "v0.2.17");

    assert!(resolve_version(&manifest, &forks(&[]), |_, _| true).is_none());
}

#[test]
fn lines_are_independent_of_each_other() {
    let manifest = live_manifest();

    let resolved = resolve_version(&manifest, &forks(&[V03]), |_, _| true).unwrap();
    assert_eq!(resolved.orig_key, "v0.3.0-rc7");
}

#[test]
fn a_pattern_selects_a_line_a_major_cannot() {
    let manifest = live_manifest();
    let pattern = regex::Regex::new(r"^v0\.2\..*$").unwrap();

    let resolved =
        resolve_version(&manifest, &all_forks(), |_, key| pattern.is_match(key)).unwrap();

    assert_eq!(resolved.orig_key, "v0.2.17");
}

#[test]
fn a_pattern_matching_nothing_resolves_to_nothing() {
    let manifest = live_manifest();
    let pattern = regex::Regex::new(r"^v9\..*$").unwrap();

    assert!(resolve_version(&manifest, &all_forks(), |_, key| pattern.is_match(key)).is_none());
}

#[test]
fn a_newer_patch_the_predicate_rejects_is_skipped() {
    let manifest = patch_manifest();
    let through_v1 = regex::Regex::new(r"^v0\.3\.[01]$").unwrap();

    let resolved =
        resolve_version(&manifest, &all_forks(), |_, key| through_v1.is_match(key)).unwrap();

    assert_eq!(resolved.orig_key, "v0.3.1");
}

#[test]
fn a_newer_patch_behind_an_inactive_fork_is_skipped() {
    let manifest = patch_manifest();

    let resolved = resolve_version(&manifest, &forks(&[V03]), |_, _| true).unwrap();
    assert_eq!(resolved.orig_key, "v0.3.1");

    let resolved = resolve_version(&manifest, &all_forks(), |_, _| true).unwrap();
    assert_eq!(resolved.orig_key, "v0.3.2");
}

#[test]
fn a_manifest_line_without_a_fork_is_rejected() {
    let err = serde_yaml::from_str::<Manifest>(
        "runner_forks: []\nexecutor_versions:\n  \"v0.3.0\": {}\n",
    )
    .err()
    .unwrap()
    .to_string();
    assert!(err.contains("fork"), "{err}");
}

#[test]
fn an_exact_version_is_the_directory_name_itself() {
    // Not in any manifest, and it still runs: the host is trusted to name a
    // line, so this is the one selector that never consults the manifest.
    assert_eq!(exact_version("my-dev-build").orig_key, "my-dev-build");
    assert_eq!(exact_version("v0.2.17").version.minor, 2);
}

fn temp_manifest(content: &str) -> std::path::PathBuf {
    let path = std::env::temp_dir().join(format!(
        "genvm-versioning-test-{}.yaml",
        rand::random::<u128>()
    ));
    std::fs::write(&path, content).unwrap();
    path
}

async fn ctx_at(manifest_path: &std::path::Path, default_network: &str) -> anyhow::Result<Ctx> {
    let config: sync::DArc<Config> = sync::DArc::new(
        serde_json::from_value(serde_json::json!({
            "threads": 1,
            "blocking_threads": 1,
            "log_disable": "",
            "manifest_path": manifest_path,
            "chainforks_path": concat!(env!("CARGO_MANIFEST_DIR"), "/../install/data/genvm-chainforks.json"),
            "default_network": default_network,
        }))
        .unwrap(),
    );
    Ctx::new(config).await
}

async fn ctx_with(manifest: &str, default_network: &str) -> anyhow::Result<Ctx> {
    let path = temp_manifest(manifest);
    let ctx = ctx_at(&path, default_network).await;
    std::fs::remove_file(path).unwrap();
    ctx
}

#[tokio::test]
async fn startup_checks_the_manifest_and_default_network_against_the_schedule() {
    let manifest =
        format!("runner_forks: []\nexecutor_versions:\n  \"v0.3.0\":\n    fork: {V03}\n");
    ctx_with(&manifest, "dev").await.unwrap();

    let err = format!("{:#}", ctx_with(&manifest, "nowhere").await.err().unwrap());
    assert!(err.contains("unknown network `nowhere`"), "{err}");

    let undefined =
        "runner_forks: []\nexecutor_versions:\n  \"v0.3.0\":\n    fork: genvm/executor/v9\n";
    let err = format!("{:#}", ctx_with(undefined, "dev").await.err().unwrap());
    assert!(
        err.contains("fork `genvm/executor/v9` is not defined"),
        "{err}"
    );

    let undefined_runner = format!(
        "runner_forks: [{V03}, {V03}/runners-x]\nexecutor_versions:\n  \"v0.3.0\":\n    fork: {V03}\n"
    );
    let err = format!(
        "{:#}",
        ctx_with(&undefined_runner, "dev").await.err().unwrap()
    );
    assert!(
        err.contains("runner fork in manifest")
            && err.contains("fork `genvm/executor/v0.3/runners-x` is not defined"),
        "{err}"
    );
}

#[test]
fn a_manifest_without_runner_forks_is_rejected() {
    let err = serde_yaml::from_str::<Manifest>(&format!(
        "executor_versions:\n  \"v0.3.0\":\n    fork: {V03}\n"
    ))
    .err()
    .unwrap()
    .to_string();
    assert!(err.contains("runner_forks"), "{err}");
}

#[tokio::test]
async fn a_rejected_reload_keeps_the_previous_manifest() {
    let path = temp_manifest(&format!(
        "runner_forks: []\nexecutor_versions:\n  \"v0.3.0\":\n    fork: {V03}\n"
    ));
    let ctx = ctx_at(&path, "dev").await.unwrap();

    std::fs::write(
        &path,
        "runner_forks: []\nexecutor_versions:\n  \"v0.3.1\":\n    fork: genvm/executor/v9\n",
    )
    .unwrap();
    let err = format!("{:#}", ctx.reload_manifest().await.unwrap_err());
    std::fs::remove_file(path).unwrap();

    assert!(
        err.contains("fork `genvm/executor/v9` is not defined"),
        "{err}"
    );
    let forks = ctx
        .active_forks("dev", "2025-01-01T00:00:00Z".parse().unwrap())
        .unwrap();
    assert_eq!(
        ctx.get_newest_version(&forks).await.unwrap().orig_key,
        "v0.3.0"
    );
}
