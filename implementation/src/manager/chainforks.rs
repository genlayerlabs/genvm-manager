use std::collections::{BTreeMap, BTreeSet};

/// Built-in fork in every active set; also the date meaning "at every timestamp".
pub const ALWAYS: &str = "always";
/// Built-in fork in no active set; also the date meaning "not on this network".
pub const NEVER: &str = "never";

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("malformed chainforks schedule: {0}")]
    Parse(#[from] serde_json::Error),
    #[error("malformed fork name `{0}`")]
    MalformedName(String),
    #[error("`{0}` is a built-in fork and cannot be defined")]
    Reserved(String),
    #[error("fork `{0}` is not defined")]
    Undefined(String),
    #[error("fork `{fork}` requires undefined fork `{required}`")]
    UndefinedRequirement { fork: String, required: String },
    #[error("fork `{0}` requires itself, directly or transitively")]
    Cycle(String),
    #[error("fork `{fork}` extends `{parent}` but does not require it")]
    MissingParent { fork: String, parent: String },
    #[error("network `{network}` does not schedule fork `{fork}`")]
    Unscheduled { network: String, fork: String },
    #[error("network `{network}` schedules undefined fork `{fork}`")]
    UndefinedScheduled { network: String, fork: String },
    #[error("network `{network}` activates `{fork}` before its requirement `{required}`")]
    BeforeRequirement {
        network: String,
        fork: String,
        required: String,
    },
    #[error("unknown network `{0}`")]
    UnknownNetwork(String),
}

/// When a fork activates on a network; ordered from earliest to latest.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Activation {
    Always,
    At(chrono::DateTime<chrono::Utc>),
    Never,
}

impl Activation {
    fn is_active_at(self, timestamp: chrono::DateTime<chrono::Utc>) -> bool {
        match self {
            Activation::Always => true,
            Activation::At(at) => at <= timestamp,
            Activation::Never => false,
        }
    }
}

impl<'de> serde::Deserialize<'de> for Activation {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let s = String::deserialize(deserializer)?;
        // Stricter than chrono, matching the build-time check: `T`, `Z` and no leap second
        static TIMESTAMP: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
            regex::Regex::new(
                r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]+)?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$",
            )
            .unwrap()
        });
        let malformed = || {
            serde::de::Error::custom(format!(
                "`{s}` is neither `always`, `never` nor an RFC 3339 timestamp"
            ))
        };
        match s.as_str() {
            ALWAYS => Ok(Activation::Always),
            NEVER => Ok(Activation::Never),
            _ if !TIMESTAMP.is_match(&s) => Err(malformed()),
            _ => chrono::DateTime::parse_from_rfc3339(&s)
                .map(|at| Activation::At(at.with_timezone(&chrono::Utc)))
                .map_err(|_| malformed()),
        }
    }
}

#[derive(Debug, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Fork {
    #[serde(default)]
    pub requires: Vec<String>,
    pub description: Option<String>,
}

#[derive(Debug, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Network {
    #[serde(deserialize_with = "unique_map")]
    pub forks: BTreeMap<String, Activation>,
}

#[derive(Debug, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Schedule {
    #[serde(deserialize_with = "unique_map")]
    pub forks: BTreeMap<String, Fork>,
    #[serde(deserialize_with = "unique_map")]
    pub networks: BTreeMap<String, Network>,
}

/// The forks active for one run; `always` and `never` are answered implicitly.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Active(BTreeSet<String>);

impl Active {
    pub fn contains(&self, fork: &str) -> bool {
        fork == ALWAYS || self.0.contains(fork)
    }
}

/// The executor's `--forks` value: sorted, comma-separated, without built-ins.
impl std::fmt::Display for Active {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        for (i, fork) in self.0.iter().enumerate() {
            if i != 0 {
                f.write_str(",")?;
            }
            f.write_str(fork)?;
        }
        Ok(())
    }
}

/// `/`-separated segments of `[a-z0-9._-]`, so a name never needs escaping in `--forks`.
pub fn is_valid_name(name: &str) -> bool {
    name.split('/').all(|segment| {
        !segment.is_empty()
            && segment
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"._-".contains(&b))
    })
}

impl Schedule {
    pub fn parse(json: &str) -> Result<Self, Error> {
        let schedule: Schedule = serde_json::from_str(json)?;
        schedule.validate()?;
        Ok(schedule)
    }

    pub fn check_defined(&self, fork: &str) -> Result<(), Error> {
        if self.forks.contains_key(fork) {
            Ok(())
        } else {
            Err(Error::Undefined(fork.to_owned()))
        }
    }

    pub fn check_network(&self, network: &str) -> Result<(), Error> {
        self.network(network).map(|_| ())
    }

    pub fn active(
        &self,
        network: &str,
        timestamp: chrono::DateTime<chrono::Utc>,
    ) -> Result<Active, Error> {
        let network = self.network(network)?;
        Ok(Active(
            network
                .forks
                .iter()
                .filter(|(_, activation)| activation.is_active_at(timestamp))
                .map(|(fork, _)| fork.clone())
                .collect(),
        ))
    }

    fn network(&self, network: &str) -> Result<&Network, Error> {
        self.networks
            .get(network)
            .ok_or_else(|| Error::UnknownNetwork(network.to_owned()))
    }

    fn validate(&self) -> Result<(), Error> {
        for (name, fork) in &self.forks {
            if name == ALWAYS || name == NEVER {
                return Err(Error::Reserved(name.clone()));
            }
            if !is_valid_name(name) {
                return Err(Error::MalformedName(name.clone()));
            }
            if let Some(required) = fork.requires.iter().find(|r| !self.forks.contains_key(*r)) {
                return Err(Error::UndefinedRequirement {
                    fork: name.clone(),
                    required: required.clone(),
                });
            }
        }

        let mut closures = BTreeMap::new();
        for name in self.forks.keys() {
            closures.insert(name.as_str(), self.requirement_closure(name)?);
        }

        for (name, closure) in &closures {
            let mut parent = *name;
            while let Some((prefix, _)) = parent.rsplit_once('/') {
                parent = prefix;
                if self.forks.contains_key(parent) && !closure.contains(parent) {
                    return Err(Error::MissingParent {
                        fork: (*name).to_owned(),
                        parent: parent.to_owned(),
                    });
                }
            }
        }

        for (network_name, network) in &self.networks {
            if let Some(fork) = network.forks.keys().find(|f| !self.forks.contains_key(*f)) {
                return Err(Error::UndefinedScheduled {
                    network: network_name.clone(),
                    fork: fork.clone(),
                });
            }
            for (name, fork) in &self.forks {
                let Some(activation) = network.forks.get(name) else {
                    return Err(Error::Unscheduled {
                        network: network_name.clone(),
                        fork: name.clone(),
                    });
                };
                if let Some(required) = fork
                    .requires
                    .iter()
                    .find(|r| network.forks.get(*r).is_some_and(|req| req > activation))
                {
                    return Err(Error::BeforeRequirement {
                        network: network_name.clone(),
                        fork: name.clone(),
                        required: required.clone(),
                    });
                }
            }
        }

        Ok(())
    }

    /// Every fork `name` requires, directly or transitively.
    fn requirement_closure<'a>(&'a self, name: &'a str) -> Result<BTreeSet<&'a str>, Error> {
        let mut seen = BTreeSet::new();
        let mut stack: Vec<&str> = self.forks[name]
            .requires
            .iter()
            .map(String::as_str)
            .collect();
        while let Some(required) = stack.pop() {
            if required == name {
                return Err(Error::Cycle(name.to_owned()));
            }
            if seen.insert(required) {
                stack.extend(self.forks[required].requires.iter().map(String::as_str));
            }
        }
        Ok(seen)
    }
}

/// A map that rejects a repeated key instead of keeping the last one.
fn unique_map<'de, D, V>(deserializer: D) -> Result<BTreeMap<String, V>, D::Error>
where
    D: serde::Deserializer<'de>,
    V: serde::Deserialize<'de>,
{
    struct Visitor<V>(std::marker::PhantomData<V>);

    impl<'de, V: serde::Deserialize<'de>> serde::de::Visitor<'de> for Visitor<V> {
        type Value = BTreeMap<String, V>;

        fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
            f.write_str("a map")
        }

        fn visit_map<A: serde::de::MapAccess<'de>>(
            self,
            mut map: A,
        ) -> Result<Self::Value, A::Error> {
            let mut out = BTreeMap::new();
            while let Some((key, value)) = map.next_entry::<String, V>()? {
                if out.contains_key(&key) {
                    return Err(serde::de::Error::custom(format!("duplicate key `{key}`")));
                }
                out.insert(key, value);
            }
            Ok(out)
        }
    }

    deserializer.deserialize_map(Visitor(std::marker::PhantomData))
}

#[cfg(test)]
#[path = "chainforks_test.rs"]
mod tests;
