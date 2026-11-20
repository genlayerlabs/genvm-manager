use genlayer_calldata::Address;

#[derive(
    Clone,
    serde::Deserialize,
    serde::Serialize,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
    Copy,
    PartialEq,
    Eq,
    Debug,
)]
pub enum On {
    #[serde(rename = "finalized")]
    #[calldata(rename = "finalized")]
    Finalized,
    #[serde(rename = "decided")]
    #[calldata(rename = "decided")]
    Decided,
}

#[derive(
    Debug,
    Clone,
    Copy,
    PartialEq,
    Eq,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
    serde::Serialize,
    serde::Deserialize,
)]
pub struct CallKey(
    #[calldata(
        serialize_with = ::genlayer_calldata::codec::as_bytes::serialize,
        deserialize_with = ::genlayer_calldata::codec::as_bytes::deserialize,
    )]
    pub [u8; 32],
);

#[derive(
    Debug,
    Clone,
    serde::Serialize,
    serde::Deserialize,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
)]
pub struct MessageData {
    pub contract_address: Address,
    pub sender_address: Address,
    pub origin_address: Address,
    pub signer_address: Address,

    pub chain_id: num_bigint::BigInt,
    pub value: num_bigint::BigInt,
    /// Deprecated; the executor derives it from the presence of code and refuses a mismatch
    #[serde(default)]
    #[calldata(default = Option::default)]
    pub is_init: Option<bool>,
    /// Transaction timestamp
    #[serde(
        default = "default_transaction_timestamp",
        with = "::genlayer_calldata::codec::rfc3339::serde"
    )]
    #[calldata(
        serialize_with = ::genlayer_calldata::codec::rfc3339::serialize,
        deserialize_with = ::genlayer_calldata::codec::rfc3339::deserialize
    )]
    #[calldata(default = default_transaction_timestamp)]
    pub transaction_timestamp: jiff::Timestamp,
}

/// Inputs of the settlement chain's CREATE2 address derivation for salted deploys
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
pub struct Create2Data {
    pub deployer: Address,
    #[calldata(
        serialize_with = ::genlayer_calldata::codec::as_bytes::serialize,
        deserialize_with = ::genlayer_calldata::codec::as_bytes::deserialize,
    )]
    pub code_hash: [u8; 32],
}

fn default_transaction_timestamp() -> jiff::Timestamp {
    genlayer_calldata::codec::rfc3339::parse("2024-11-26T06:42:42.424242Z").unwrap()
}
