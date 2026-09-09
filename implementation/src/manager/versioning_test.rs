use super::*;

/// The manifest the manager ships: two live lines, both semver major zero.
fn live_manifest() -> Manifest {
    serde_yaml::from_str(
        "executor_versions:\n  \"v0.2.17\":\n    available_after: 2024-09-01T00:00:00Z\n  \"v0.3.0-rc7\":\n    available_after: 2024-09-01T00:00:00Z\n",
    )
    .unwrap()
}

fn now() -> chrono::DateTime<chrono::Utc> {
    "2025-01-01T00:00:00Z".parse().unwrap()
}

#[test]
fn a_major_cannot_separate_two_zero_major_lines() {
    let manifest = live_manifest();

    // Both keys are major 0, so the only major matching anything matches both
    // and the newest wins. This is why a version string exists.
    let resolved = resolve_version(&manifest, now(), |ver, _| ver.major == 0).unwrap();
    assert_eq!(resolved.orig_key, "v0.3.0-rc7");

    assert!(resolve_version(&manifest, now(), |ver, _| ver.major == 2).is_none());
}

#[test]
fn a_pattern_selects_a_line_a_major_cannot() {
    let manifest = live_manifest();
    let pattern = regex::Regex::new(r"^v0\.2\..*$").unwrap();

    let resolved = resolve_version(&manifest, now(), |_, key| pattern.is_match(key)).unwrap();

    assert_eq!(resolved.orig_key, "v0.2.17");
}

#[test]
fn a_pattern_matching_nothing_resolves_to_nothing() {
    let manifest = live_manifest();
    let pattern = regex::Regex::new(r"^v9\..*$").unwrap();

    assert!(resolve_version(&manifest, now(), |_, key| pattern.is_match(key)).is_none());
}

#[test]
fn the_patch_walk_stops_where_the_predicate_stops() {
    let manifest: Manifest = serde_yaml::from_str(
        "executor_versions:\n  \"v0.3.0\":\n    available_after: 2024-09-01T00:00:00Z\n  \"v0.3.1\":\n    available_after: 2024-09-01T00:00:00Z\n  \"v0.3.2\":\n    available_after: 2024-09-01T00:00:00Z\n",
    )
    .unwrap();
    let through_v1 = regex::Regex::new(r"^v0\.3\.[01]$").unwrap();

    // Without the predicate on the walk this slides to v0.3.2, which the caller
    // asked not to run.
    let resolved = resolve_version(&manifest, now(), |_, key| through_v1.is_match(key)).unwrap();

    assert_eq!(resolved.orig_key, "v0.3.1");
}

#[test]
fn an_exact_version_is_the_directory_name_itself() {
    // Not in any manifest, and it still runs: the host is trusted to name a
    // line, so this is the one selector that never consults the manifest.
    assert_eq!(exact_version("my-dev-build").orig_key, "my-dev-build");
    assert_eq!(exact_version("v0.2.17").version.minor, 2);
}
