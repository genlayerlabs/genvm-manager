use genvm_modules::common::{LogSinkElement, LogSinkInner, INTROSPECTOR_DROPPED, LOG_SINK_LIMIT};

type Record = serde_json::Map<String, serde_json::Value>;

fn entry(audience: &str, seq: usize) -> LogSinkElement {
    LogSinkElement::Map(serde_json::Map::from_iter([
        (
            "audience".into(),
            serde_json::Value::String(audience.into()),
        ),
        ("seq".into(), seq.into()),
    ]))
}

fn fill(sink: &LogSinkInner, audience: &str, count: usize) {
    for seq in 0..count {
        sink.push(entry(audience, seq));
    }
}

fn audiences(drained: &[Record]) -> Vec<String> {
    drained
        .iter()
        .map(|x| x["audience"].as_str().unwrap().to_owned())
        .collect()
}

fn markers(drained: &[Record]) -> usize {
    drained
        .iter()
        .filter(|x| x.get("message").and_then(|m| m.as_str()) == Some(INTROSPECTOR_DROPPED))
        .count()
}

#[test]
fn fresh_sink_keeps_every_entry_under_the_cap() {
    let sink = LogSinkInner::new(false);
    fill(&sink, "introspector", LOG_SINK_LIMIT - 1);

    let drained = sink.drain();
    assert_eq!(drained.len(), LOG_SINK_LIMIT - 1);
    assert_eq!(markers(&drained), 0);
}

#[test]
fn cap_hit_drops_introspector_entries_and_adds_a_marker() {
    let sink = LogSinkInner::new(false);
    for seq in 0..LOG_SINK_LIMIT {
        let audience = if seq % 4 == 0 { "user" } else { "introspector" };
        sink.push(entry(audience, seq));
    }
    sink.push(entry("operator", LOG_SINK_LIMIT));

    let drained = sink.drain();
    let auds = audiences(&drained);

    assert!(
        !auds.contains(&"introspector".to_owned()),
        "introspector entries survived: {auds:?}"
    );
    assert_eq!(
        markers(&drained),
        1,
        "expected exactly one marker: {auds:?}"
    );
    assert_eq!(
        drained.last().unwrap()["seq"].as_u64(),
        Some(LOG_SINK_LIMIT as u64),
        "the incoming entry must be kept"
    );
    assert_eq!(
        auds.iter().filter(|x| *x == "user").count(),
        LOG_SINK_LIMIT / 4
    );
}

#[test]
fn compacted_sink_discards_introspector_entries_at_push() {
    let sink = LogSinkInner::new(false);
    // the last one triggers the compaction and is compacted away with the rest
    fill(&sink, "introspector", LOG_SINK_LIMIT + 1);

    sink.push(entry("introspector", 1000));
    sink.push(entry("user", 1001));
    sink.push(entry("operator", 1002));

    let drained = sink.drain();
    let auds = audiences(&drained);

    assert_eq!(
        auds,
        vec![
            "operator".to_owned(), // the marker
            "user".to_owned(),
            "operator".to_owned(),
        ],
        "only the marker and the later non-introspector entries stay"
    );
}

#[test]
fn second_cap_hit_drops_the_oldest_entry_of_any_audience() {
    let sink = LogSinkInner::new(false);
    fill(&sink, "user", LOG_SINK_LIMIT);

    // overflows while fresh: nothing to compact, so the marker and this entry each
    // cost an oldest entry
    sink.push(entry("user", 1000));
    sink.push(entry("user", 1001));

    let drained = sink.drain();
    assert_eq!(drained.len(), LOG_SINK_LIMIT);
    // three slots were needed: the marker and the two entries
    assert_eq!(
        drained[0]["seq"].as_u64(),
        Some(3),
        "the oldest entries must be the ones dropped"
    );
    assert_eq!(drained.last().unwrap()["seq"].as_u64(), Some(1001));
}

#[test]
fn a_chatty_run_never_grows_past_the_cap() {
    let sink = LogSinkInner::new(false);
    for seq in 0..LOG_SINK_LIMIT * 100 {
        let audience = match seq % 3 {
            0 => "introspector",
            1 => "user",
            _ => "operator",
        };
        sink.push(entry(audience, seq));
        assert!(
            sink.buffered() <= LOG_SINK_LIMIT,
            "sink grew to {} at {seq}",
            sink.buffered()
        );
    }
    fill(&sink, "introspector", LOG_SINK_LIMIT * 10);

    let drained = sink.drain();
    assert_eq!(drained.len(), LOG_SINK_LIMIT);
    assert!(
        !audiences(&drained).contains(&"introspector".to_owned()),
        "introspector entries came back once the fifo phase started"
    );
    assert_eq!(
        drained.last().unwrap()["seq"].as_u64(),
        Some((LOG_SINK_LIMIT * 100 - 1) as u64),
        "the newest entry must be kept"
    );
}

#[test]
fn debug_sink_never_evicts() {
    let sink = LogSinkInner::new(true);
    fill(&sink, "introspector", LOG_SINK_LIMIT * 3);

    let drained = sink.drain();
    assert_eq!(drained.len(), LOG_SINK_LIMIT * 3);
    assert_eq!(markers(&drained), 0);
}

#[test]
fn an_entry_without_an_audience_is_an_introspector_one() {
    let sink = LogSinkInner::new(false);
    sink.push(LogSinkElement::Map(serde_json::Map::from_iter([(
        "level".into(),
        serde_json::Value::String("info".into()),
    )])));
    sink.push(LogSinkElement::Raw(br#"{"level":"info"}"#.to_vec()));

    let drained = sink.drain();
    assert_eq!(audiences(&drained), vec!["introspector", "introspector"]);
}

/// Neither shape is our JSON: the node runner is the one who can act on it
#[test]
fn a_line_the_executor_did_not_format_is_an_operator_one() {
    let sink = LogSinkInner::new(false);
    sink.push(LogSinkElement::Line("raw text".into()));
    sink.push(LogSinkElement::Raw(b"\xff not json".to_vec()));

    let drained = sink.drain();
    assert_eq!(audiences(&drained), vec!["operator", "operator"]);
    assert_eq!(drained[0]["line"].as_str(), Some("raw text"));
}

#[test]
fn a_garbage_audience_is_replaced_and_the_leading_keys_are_ordered() {
    let sink = LogSinkInner::new(false);
    sink.push(LogSinkElement::Map(serde_json::Map::from_iter([
        ("message".into(), serde_json::Value::String("m".into())),
        ("audience".into(), serde_json::Value::from(7)),
        ("level".into(), serde_json::Value::String("warn".into())),
    ])));

    let drained = sink.drain();
    assert_eq!(
        drained[0].keys().collect::<Vec<_>>(),
        vec!["level", "audience", "message"]
    );
    assert_eq!(drained[0]["audience"].as_str(), Some("introspector"));
}
