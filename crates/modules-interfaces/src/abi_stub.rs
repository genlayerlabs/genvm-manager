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
#[cfg_attr(feature = "arbitrary", derive(arbitrary::Arbitrary))]
pub enum On {
    #[serde(rename = "finalized")]
    #[calldata(rename = "finalized")]
    Finalized,
    #[serde(rename = "accepted")]
    #[calldata(rename = "accepted")]
    Accepted,
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

#[cfg(feature = "arbitrary")]
impl<'a> arbitrary::Arbitrary<'a> for MessageData {
    fn arbitrary(u: &mut arbitrary::Unstructured<'a>) -> arbitrary::Result<Self> {
        use arbitrary::Arbitrary;

        let ts = u32::arbitrary(u)?;
        let Some(datetime) = chrono::DateTime::<chrono::Utc>::from_timestamp_secs(ts as i64) else {
            return Err(arbitrary::Error::NotEnoughData);
        };

        let chain_id_bytes: [u8; 32] = Arbitrary::arbitrary(u)?;
        let chain_id = primitive_types::U256::from_big_endian(&chain_id_bytes);

        let value_bytes: [u8; 32] = Arbitrary::arbitrary(u)?;
        let value = primitive_types::U256::from_big_endian(&value_bytes);

        Ok(Self {
            contract_address: Arbitrary::arbitrary(u)?,
            sender_address: Arbitrary::arbitrary(u)?,
            origin_address: Arbitrary::arbitrary(u)?,
            signer_address: Arbitrary::arbitrary(u)?,
            chain_id: num_bigint::BigInt::from_bytes_be(
                num_bigint::Sign::Plus,
                &chain_id.to_big_endian(),
            ),
            value: num_bigint::BigInt::from_bytes_be(
                num_bigint::Sign::Plus,
                &value.to_big_endian(),
            ),
            is_init: bool::arbitrary(u)?,
            datetime,
        })
    }
}
