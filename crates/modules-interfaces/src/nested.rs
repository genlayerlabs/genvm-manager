use bytes::Bytes;
use genlayer_calldata::codec::{Decode, DecodeError, Deserializer, Encode};

use crate::{MessageData, ResultCode};

/// Prefix marking a version string as a regular expression rather than a
/// directory name. Universal: it holds wherever a version is named, from a
/// nested run's routing to a request's debug reroute.
pub const VERSION_REGEX_PREFIX: &str = "re:";

/// What a version string names, once [`VERSION_REGEX_PREFIX`] is accounted for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VersionMatch<'a> {
    /// An executor directory, used as it stands.
    Exact(&'a str),
    /// A pattern over manifest version keys, resolved by the manifest's rules.
    Regex(&'a str),
}

/// Reads a version string by the universal rule.
pub fn parse_version_match(version: &str) -> VersionMatch<'_> {
    match version.strip_prefix(VERSION_REGEX_PREFIX) {
        Some(pattern) => VersionMatch::Regex(pattern),
        None => VersionMatch::Exact(version),
    }
}

/// Which executor line a run must use: a major, left to the manifest's rules,
/// or a version naming one directly by [`parse_version_match`].
///
/// As a nested run's routing this is normally minted by the host, which executor
/// lines carry without reading. An executor mints one itself only for a callee
/// whose major it does not serve and the host declined to place, because the
/// mapping from a major to a line belongs to the manager rather than to any
/// line.
#[derive(
    Debug,
    Clone,
    PartialEq,
    Eq,
    serde::Serialize,
    serde::Deserialize,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
)]
#[calldata(tag = "kind")]
#[serde(tag = "kind")]
pub enum ExecutorSelector {
    #[calldata(rename = "major")]
    #[serde(rename = "major")]
    MajorOverride { major: u32 },
    #[calldata(rename = "version")]
    #[serde(rename = "version")]
    VersionOverride { version: String },
}

/// Storage view carried across an executor boundary.
///
/// Executor lines map variants by meaning to their local `StorageType`; they
/// must not cast local discriminants to or from this type.
#[derive(
    Debug,
    Clone,
    Copy,
    PartialEq,
    Eq,
    serde::Serialize,
    serde::Deserialize,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
)]
#[serde(rename_all = "snake_case")]
pub enum NestedStorageType {
    Default,
    LatestFinal,
    LatestNonFinal,
}

/// Permission bits carried across an executor boundary.
///
/// Each executor maps its local permission fields by meaning. Bits unknown to
/// this shared representation are cleared while decoding and therefore deny
/// rather than grant a permission. `READ_STORAGE` maps the legacy line's
/// separate read flag and is ignored by lines where reads are not gated;
/// permissions with no local equivalent are likewise left denied.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, serde::Serialize)]
#[serde(transparent)]
pub struct NestedPermissions(u32);

impl NestedPermissions {
    pub const DETERMINISTIC: Self = Self(1 << 0);
    pub const READ_STORAGE: Self = Self(1 << 1);
    pub const WRITE_STORAGE: Self = Self(1 << 2);
    pub const SEND_MESSAGES: Self = Self(1 << 3);
    pub const CALL_OTHERS: Self = Self(1 << 4);
    pub const SPAWN_NONDET: Self = Self(1 << 5);
    pub const REGISTER_RUNNERS: Self = Self(1 << 6);
    pub const USE_BALANCE_FOR_MESSAGE_FEES: Self = Self(1 << 7);

    const KNOWN_BITS: u32 = Self::DETERMINISTIC.0
        | Self::READ_STORAGE.0
        | Self::WRITE_STORAGE.0
        | Self::SEND_MESSAGES.0
        | Self::CALL_OTHERS.0
        | Self::SPAWN_NONDET.0
        | Self::REGISTER_RUNNERS.0
        | Self::USE_BALANCE_FOR_MESSAGE_FEES.0;

    pub const fn from_bits(bits: u32) -> Self {
        Self(bits & Self::KNOWN_BITS)
    }

    pub const fn bits(self) -> u32 {
        self.0
    }

    pub const fn contains(self, permission: Self) -> bool {
        self.0 & permission.0 == permission.0
    }
}

impl std::ops::BitOr for NestedPermissions {
    type Output = Self;

    fn bitor(self, rhs: Self) -> Self::Output {
        Self(self.0 | rhs.0)
    }
}

impl std::ops::BitOrAssign for NestedPermissions {
    fn bitor_assign(&mut self, rhs: Self) {
        self.0 |= rhs.0;
    }
}

impl<'de> serde::Deserialize<'de> for NestedPermissions {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        u32::deserialize(deserializer).map(Self::from_bits)
    }
}

impl<W: genlayer_calldata::Writer> Encode<W> for NestedPermissions {
    type Error = W::Error;

    fn encode(&self, encoder: &mut genlayer_calldata::Encoder<W>) -> Result<(), Self::Error> {
        self.0.encode(encoder)
    }
}

impl Decode for NestedPermissions {
    fn decode<D: Deserializer>(deserializer: D) -> Result<Self, DecodeError> {
        u32::decode(deserializer).map(Self::from_bits)
    }
}

/// Textual runner reference carried across an executor boundary.
///
/// A `CallContract` child uses `contract`, which the receiving executor
/// resolves against the callee code slot using its own storage layout. Local
/// enum encodings and caller-derived code slots are never placed on this
/// boundary.
#[derive(
    Debug,
    Clone,
    PartialEq,
    Eq,
    serde::Serialize,
    serde::Deserialize,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
)]
#[serde(transparent)]
pub struct NestedRunnerId(pub String);

/// Complete input needed to run a `CallContract` child in another executor.
///
/// Carries no wire version: every executor line compiles this very definition,
/// so a change is a compile error at each construction site rather than
/// something a peer could observe at runtime.
#[derive(
    Debug,
    Clone,
    serde::Serialize,
    serde::Deserialize,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
)]
pub struct NestedRunEnvelope {
    /// Host-minted routing data, opaque to both executor lines.
    pub routing_payload: Bytes,
    pub calldata: Bytes,
    /// The caller sets `value` to zero and `is_init` to false.
    pub message: MessageData,
    /// View-call stack with the caller's contract address appended.
    pub stack: Vec<genlayer_calldata::Address>,
    pub permissions: NestedPermissions,
    pub state_mode: NestedStorageType,
    pub topmost_runner_id: NestedRunnerId,
    /// Recursion budget left for the whole chain, minted by its root. Carried
    /// as a remainder rather than a spent count so the bound does not depend
    /// on each line's own `VM_RECURSION`.
    pub remaining_recursion: u32,
    pub remaining_det_fuel: primitive_types::U256,
    pub memory_limit: u32,
}

/// ABI-neutral result of a nested contract call.
#[derive(Debug, Clone, genlayer_calldata::Encode, genlayer_calldata::Decode)]
pub struct NestedRunResult {
    pub kind: ResultCode,
    pub data: genlayer_calldata::unparsed::Maybe<genlayer_calldata::Value>,
}

/// Result returned to the caller executor after a nested run.
#[derive(Debug, Clone, genlayer_calldata::Encode, genlayer_calldata::Decode)]
pub struct NestedRunReply {
    pub result: NestedRunResult,
    /// The callee's [`crate::small_hash`], forwarded unchanged, and the only
    /// hash the caller folds: an execution hash also commits to fee accounting
    /// and storage effects, which would make the same call hash differently
    /// depending on whether the host routed it in-process or across majors.
    pub small_hash: Bytes,
    /// True only when the reported result proves that no effects were produced.
    pub effect_free: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_permission_bits_are_denied() {
        let encoded =
            genlayer_calldata::encode_obj(&(NestedPermissions::CALL_OTHERS.bits() | (1 << 31)));
        let decoded: NestedPermissions = genlayer_calldata::decode_obj(&encoded).unwrap();

        assert_eq!(decoded.bits(), NestedPermissions::CALL_OTHERS.bits());
    }

    #[test]
    fn small_hash_separates_outcome_kinds() {
        use crate::domain::{small_hash, ResultCode, WasmStoreHashes};

        let data = genlayer_calldata::Value::Str("x".to_owned());
        let hash = |kind| small_hash(kind, &data, &[0u8; 32], &WasmStoreHashes::default());

        assert_ne!(hash(ResultCode::Return), hash(ResultCode::UserError));
        assert_ne!(hash(ResultCode::Return), hash(ResultCode::VmError));
        // Neither an internal error nor fatality changes the outcome value, so
        // both fold as a plain `VMError` rather than adding a kind a peer line
        // might not know.
        assert_eq!(hash(ResultCode::VmError), hash(ResultCode::InternalError));
        assert_eq!(hash(ResultCode::VmError), hash(ResultCode::FatalVmError));
    }

    #[test]
    fn routing_payload_round_trips() {
        for selector in [
            ExecutorSelector::MajorOverride { major: 3 },
            ExecutorSelector::VersionOverride {
                version: "v0.2.17".to_owned(),
            },
            ExecutorSelector::VersionOverride {
                version: "re:^v0\\.2\\..*$".to_owned(),
            },
        ] {
            let encoded = genlayer_calldata::encode_obj(&selector);
            let decoded: ExecutorSelector = genlayer_calldata::decode_obj(&encoded).unwrap();

            assert_eq!(decoded, selector);
        }
    }

    #[test]
    fn a_version_string_carries_its_own_rule() {
        assert_eq!(
            parse_version_match("v0.2.17"),
            VersionMatch::Exact("v0.2.17")
        );
        assert_eq!(
            parse_version_match("re:^v0\\.2\\..*$"),
            VersionMatch::Regex("^v0\\.2\\..*$")
        );
    }
}
