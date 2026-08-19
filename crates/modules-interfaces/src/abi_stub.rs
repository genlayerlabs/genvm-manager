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
    pub is_init: bool,
    /// Transaction timestamp
    #[serde(default = "default_datetime")]
    #[calldata(
        serialize_with = encode_datetime_rfc3339,
        deserialize_with = decode_datetime_rfc3339
    )]
    #[calldata(default = default_datetime)]
    pub datetime: chrono::DateTime<chrono::Utc>,
}

fn decode_datetime_rfc3339(
    val: genlayer_calldata::Value,
) -> Result<chrono::DateTime<chrono::Utc>, genlayer_calldata::codec::DecodeError> {
    let genlayer_calldata::Value::Str(s) = val else {
        return Err(genlayer_calldata::codec::DecodeError::Unexpected(
            "expected string for datetime",
        ));
    };
    chrono::DateTime::parse_from_rfc3339(&s)
        .map(|dt| dt.to_utc())
        .map_err(|e| genlayer_calldata::codec::DecodeError::UserError(Box::new(e)))
}

fn encode_datetime_rfc3339<W: genlayer_calldata::Writer>(
    dt: &chrono::DateTime<chrono::Utc>,
    enc: &mut genlayer_calldata::Encoder<W>,
) -> Result<(), W::Error> {
    use chrono::SecondsFormat;
    let s = dt.to_rfc3339_opts(SecondsFormat::AutoSi, true);
    enc.push_str(&s)
}

fn default_datetime() -> chrono::DateTime<chrono::Utc> {
    chrono::DateTime::parse_from_rfc3339("2024-11-26T06:42:42.424242Z")
        .unwrap()
        .to_utc()
}
