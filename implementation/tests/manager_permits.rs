use genvm_modules::manager::{PermitsConfig, PermitsPerGig};

fn per_gib(yaml: &str) -> PermitsPerGig {
    parse(&format!("per_gib: {yaml}")).per_gib
}

fn parse(yaml: &str) -> PermitsConfig {
    serde_yaml::from_str(yaml).unwrap()
}

fn parse_err(yaml: &str) -> String {
    serde_yaml::from_str::<PermitsConfig>(&format!("per_gib: {yaml}"))
        .unwrap_err()
        .to_string()
}

#[test]
fn a_ratio_is_exact() {
    assert_eq!(per_gib("1/4").apply(16), 4);
    assert_eq!(per_gib("1/4").apply(15), 3);
    assert_eq!(per_gib("2/3").apply(10), 6);
}

#[test]
fn an_integer_is_a_whole_ratio() {
    assert_eq!(per_gib("1").apply(7), 7);
    assert_eq!(per_gib("3").apply(7), 21);
    assert_eq!(per_gib("'3/1'").to_string(), "3/1");
}

#[test]
fn defaults_hand_out_one_permit_per_gibibyte() {
    let config = parse("{}");
    assert_eq!(config.per_gib.apply(12), 12);
    assert_eq!(config.total, None);
    assert_eq!(config.sync, 4);
    assert_eq!(config.nondet, 8);
}

#[test]
fn a_ratio_is_normalized_when_printed() {
    assert_eq!(per_gib("2/8").to_string(), "1/4");
}

#[test]
fn scaling_saturates_instead_of_wrapping() {
    let huge = format!("{}", u64::MAX);
    assert_eq!(per_gib(&huge).apply(usize::MAX), usize::MAX);
}

#[test]
fn a_zero_or_malformed_ratio_is_refused() {
    for (input, expected) in [
        ("0", "must be positive"),
        ("1/0", "divide by zero"),
        ("'1/'", "denominator"),
        ("'-1'", "numerator"),
        ("'1/2/3'", "denominator"),
    ] {
        let err = parse_err(input);
        assert!(
            err.contains(expected),
            "{input}: expected {expected:?}, got {err}"
        );
    }
}
