use super::*;

#[derive(clap::Parser)]
struct Top {
    #[command(subcommand)]
    command: TopCommand,
}

#[derive(clap::Subcommand)]
enum TopCommand {
    Manager(CliArgs),
}

fn parse(args: &[&str]) -> CliArgs {
    use clap::Parser as _;
    let TopCommand::Manager(cli) =
        Top::try_parse_from(["genvm-modules", "manager"].iter().chain(args))
            .unwrap()
            .command;
    cli
}

fn config_with(default_network: serde_json::Value) -> serde_json::Value {
    serde_json::json!({
        "threads": 1,
        "blocking_threads": 1,
        "log_disable": "",
        "manifest_path": "manifest.yaml",
        "chainforks_path": "chainforks.json",
        "default_network": default_network,
    })
}

fn config() -> Config {
    serde_json::from_value(config_with("dev".into())).unwrap()
}

fn config_without_default() -> Config {
    serde_json::from_value(config_with(serde_json::Value::Null)).unwrap()
}

#[test]
fn default_network_is_required_but_may_be_null() {
    assert_eq!(config_without_default().default_network, None);

    let mut absent = config_with(serde_json::Value::Null);
    absent.as_object_mut().unwrap().remove("default_network");
    let err = serde_json::from_value::<Config>(absent)
        .err()
        .unwrap()
        .to_string();
    assert!(err.contains("default_network"), "{err}");
}

#[test]
fn a_request_without_a_network_needs_a_default() {
    let without = config_without_default();
    let err = without.network(None).unwrap_err().to_string();
    assert_eq!(
        err,
        "request names no network and the manager has no default_network"
    );
    assert_eq!(without.network(Some("clarke")).unwrap(), "clarke");

    let with = config();
    assert_eq!(with.network(None).unwrap(), "dev");
    assert_eq!(with.network(Some("clarke")).unwrap(), "clarke");
}

#[test]
fn the_default_network_flag_wins_over_a_null_config() {
    let mut config = config_without_default();
    parse(&["--default-network", "clarke"]).apply_overrides(&mut config);
    assert_eq!(config.network(None).unwrap(), "clarke");
}

#[test]
fn config_and_overrides_parse_on_either_side_of_check_install() {
    let overrides = [
        "--config",
        "/node/genvm-manager.yaml",
        "--chainforks-path",
        "/node/chainforks.json",
        "--default-network",
        "bradbury-phase2",
    ];
    let after = parse(&[&["check-install"][..], &overrides].concat());
    let before = parse(&[&overrides[..], &["check-install"]].concat());

    for cli in [after, before] {
        assert!(matches!(cli.command, Some(SubCommand::CheckInstall(_))));
        assert_eq!(cli.config, "/node/genvm-manager.yaml");
        assert_eq!(
            cli.chainforks_path.as_deref(),
            Some("/node/chainforks.json")
        );
        assert_eq!(cli.default_network.as_deref(), Some("bradbury-phase2"));
    }
}

#[test]
fn schedule_overrides_parse_for_serve() {
    let cli = parse(&[
        "--die-with-parent",
        "--port",
        "4000",
        "--chainforks-path",
        "/node/chainforks.json",
        "--default-network",
        "clarke",
    ]);

    assert!(cli.command.is_none());
    assert_eq!(cli.port, 4000);
    assert_eq!(
        cli.chainforks_path.as_deref(),
        Some("/node/chainforks.json")
    );
    assert_eq!(cli.default_network.as_deref(), Some("clarke"));
}

#[test]
fn overrides_replace_only_what_they_name() {
    let mut untouched = config();
    parse(&[]).apply_overrides(&mut untouched);
    assert_eq!(untouched.manifest_path, "manifest.yaml");
    assert_eq!(untouched.chainforks_path, "chainforks.json");
    assert_eq!(untouched.default_network.as_deref(), Some("dev"));

    let mut overridden = config();
    parse(&[
        "--manifest-path",
        "/m.yaml",
        "--chainforks-path",
        "/c.json",
        "--default-network",
        "clarke",
    ])
    .apply_overrides(&mut overridden);
    assert_eq!(overridden.manifest_path, "/m.yaml");
    assert_eq!(overridden.chainforks_path, "/c.json");
    assert_eq!(overridden.default_network.as_deref(), Some("clarke"));
}
