use bytes::Bytes;
use primitive_types::U256;

pub mod fees;

fn default_record_actions() -> Vec<String> {
    Vec::new()
}

fn default_host_hello_data() -> Vec<Bytes> {
    Vec::new()
}

fn default_none<T>() -> Option<T> {
    None
}

#[derive(
    Debug,
    Clone,
    serde::Serialize,
    serde::Deserialize,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
)]
pub struct ExecutionData {
    pub calldata: Bytes,
    pub message: super::abi_stub::MessageData,
    pub host_data: String,
    pub code: Option<Bytes>,
    pub leader_nondet_results: Option<Vec<Bytes>>,
    /// Maps each host method (by index) to a host id. When empty, all methods use host 0.
    pub method_hosts: Vec<u8>,
    /// Raw bytes written to each host connection before the first method byte.
    #[serde(default)]
    #[calldata(default = default_host_hello_data)]
    pub host_hello_data: Vec<Bytes>,
    pub bucket_totals: Vec<num_bigint::BigInt>,
    /// Host-provided `node` fee constants (moved off `host_data`).
    pub gas_data: std::collections::BTreeMap<String, String>,
    /// Message-fee allocation tree passed alongside the execution.
    pub message_fee_allocation: Vec<fees::MessageAllocationNode>,
    /// Initial time-unit budget for this execution.
    pub initial_time_units_allocation: u32,
    /// Auditable supervisor action kinds to return in the execution result.
    #[serde(default)]
    #[calldata(default = default_record_actions)]
    pub record_actions: Vec<String>,
    /// Recursion budget imported by a nested execution, or forced by a debug
    /// override. Absent means the execution starts from the executor's own
    /// limit.
    #[serde(default)]
    #[calldata(default = default_none)]
    pub remaining_recursion: Option<u32>,
    /// Present exactly when this execution is nested inside another one.
    #[serde(default)]
    #[calldata(default = default_none)]
    pub nested: Option<NestedExecutionData>,
}

/// The execution state a nested run imports from the caller it was delegated
/// by.
///
/// These fields only mean anything together, so they travel together: a run is
/// nested or it is not, and there is no way to spell a half-nested one. Notably
/// *not* here is `remaining_recursion`, which a chain root can also carry.
#[derive(
    Debug,
    Clone,
    serde::Serialize,
    serde::Deserialize,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
)]
pub struct NestedExecutionData {
    /// Explicit memory budget for the nested executor process.
    pub memory_limit: u32,
    /// View-call stack imported from the caller.
    pub stack: Vec<genlayer_calldata::Address>,
    /// Permissions the caller derived for the callee.
    pub permissions: crate::NestedPermissions,
    /// Storage view the caller resolved for the callee.
    pub state_mode: crate::NestedStorageType,
    /// Entry runner the caller resolved for the callee.
    pub topmost_runner_id: crate::NestedRunnerId,
    /// Deterministic fuel left to the chain this run belongs to.
    pub remaining_det_fuel: primitive_types::U256,
}

#[allow(clippy::enum_variant_names)]
#[derive(Debug, Clone, PartialEq, Eq, genlayer_calldata::Encode, genlayer_calldata::Decode)]
#[calldata(tag = "type")]
pub enum ExecutionEmission {
    ExternalMessage {
        address: genlayer_calldata::Address,
        calldata: Bytes,
        value: U256,
        message_fee: U256,
        receipt_fee: U256,

        fee_params: fees::ExternalMessageParams,
    },
    InternalMessage {
        call_key: crate::CallKey,
        address: genlayer_calldata::Address,
        calldata: genlayer_calldata::codec::Maybe<genlayer_calldata::Value>,
        value: U256,
        on: crate::On,
        message_fee: U256,
        receipt_fee: U256,

        fee_params: fees::InternalMessageParams,
        subtree: bytes::Bytes,
        /// Chain `useBalance`: the fee is drawn from the emitting contract's
        /// balance rather than the sender's prefunded message-fee pool.
        use_balance: bool,
    },
    InternalDeployMessage {
        calldata: genlayer_calldata::codec::Maybe<genlayer_calldata::Value>,
        code: Bytes,
        value: U256,
        on: crate::On,
        salt_nonce: U256,
        message_fee: U256,
        receipt_fee: U256,

        fee_params: fees::InternalMessageParams,
        subtree: bytes::Bytes,
        /// Chain `useBalance`; see `ExecutionEmission::InternalMessage::use_balance`.
        use_balance: bool,
    },
    Event {
        topics: Vec<Bytes>,
        blob: genlayer_calldata::codec::Maybe<genlayer_calldata::Map<genlayer_calldata::Value>>,
        storage_fee: U256,
    },
}

#[derive(
    Debug,
    PartialEq,
    Clone,
    Copy,
    serde::Serialize,
    serde::Deserialize,
    ::genlayer_calldata::Encode,
    ::genlayer_calldata::Decode,
)]
#[repr(u8)]
pub enum ResultCode {
    Return = 0,
    UserError = 1,
    VmError = 2,
    InternalError = 3,
    FatalVmError = 4,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct StorageDelta(
    #[serde(with = "serde_bytes")] [u8; 36],
    #[serde(with = "serde_bytes")] Vec<u8>,
);

impl StorageDelta {
    pub fn new(key: [u8; 36], value: Vec<u8>) -> Self {
        Self(key, value)
    }
}

impl<W: genlayer_calldata::Writer> genlayer_calldata::codec::Encode<W> for StorageDelta {
    type Error = W::Error;

    fn encode(&self, enc: &mut genlayer_calldata::Encoder<W>) -> Result<(), Self::Error> {
        enc.start_array(2)?;
        enc.push_bytes(&self.0)?;
        enc.push_bytes(&self.1)
    }
}

#[derive(
    Debug,
    Clone,
    serde::Serialize,
    PartialEq,
    Eq,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
)]
pub struct Frame {
    pub module_name: String,
    pub func: u32,
}

/// The wasm call stack captured at the point of a trap.
#[derive(Debug, Clone, serde::Serialize, PartialEq, Eq)]
#[serde(transparent)]
pub struct Backtrace {
    pub frames: Vec<Frame>,
}

impl<W: genlayer_calldata::Writer> genlayer_calldata::codec::Encode<W> for Backtrace {
    type Error = W::Error;

    fn encode(&self, enc: &mut genlayer_calldata::Encoder<W>) -> Result<(), Self::Error> {
        genlayer_calldata::codec::Encode::encode(&self.frames, enc)
    }
}

/// Decode mirror of [`ModuleFingerprint`]; the encoded form uses byte strings
/// and there is no `Decode` for fixed-size arrays.
#[derive(genlayer_calldata::Decode)]
struct ModuleFingerprintDe {
    memories: Vec<Bytes>,
}

#[derive(Debug, Clone, serde::Serialize, PartialEq, Eq)]
pub struct ModuleFingerprint {
    pub memories: Vec<[u8; 32]>,
}

impl<W: genlayer_calldata::Writer> genlayer_calldata::codec::Encode<W> for ModuleFingerprint {
    type Error = W::Error;

    fn encode(&self, enc: &mut genlayer_calldata::Encoder<W>) -> Result<(), Self::Error> {
        enc.start_map(1)?;
        enc.push_map_k("memories")?;
        enc.start_array(self.memories.len() as u64)?;
        for memory in &self.memories {
            enc.push_bytes(memory)?;
        }
        Ok(())
    }
}

impl genlayer_calldata::codec::Decode for ModuleFingerprint {
    fn decode<D: genlayer_calldata::codec::Deserializer>(
        deserializer: D,
    ) -> Result<Self, genlayer_calldata::codec::DecodeError> {
        let raw = ModuleFingerprintDe::decode(deserializer)?;
        let memories = raw
            .memories
            .into_iter()
            .map(|memory| {
                <[u8; 32]>::try_from(memory.as_ref()).map_err(|_| {
                    genlayer_calldata::codec::DecodeError::Unexpected(
                        "module fingerprint memory hash must be 32 bytes",
                    )
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Self { memories })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct WasmStoreHashes(pub std::collections::BTreeMap<String, ModuleFingerprint>);

impl<W: genlayer_calldata::Writer> genlayer_calldata::codec::Encode<W> for WasmStoreHashes {
    type Error = W::Error;

    fn encode(&self, enc: &mut genlayer_calldata::Encoder<W>) -> Result<(), Self::Error> {
        enc.start_map(self.0.len() as u64)?;
        for (module, fingerprint) in &self.0 {
            enc.push_map_k(module)?;
            genlayer_calldata::codec::Encode::encode(fingerprint, enc)?;
        }
        Ok(())
    }
}

impl genlayer_calldata::codec::Decode for WasmStoreHashes {
    fn decode<D: genlayer_calldata::codec::Deserializer>(
        deserializer: D,
    ) -> Result<Self, genlayer_calldata::codec::DecodeError> {
        std::collections::BTreeMap::<String, ModuleFingerprint>::decode(deserializer).map(Self)
    }
}

impl genlayer_calldata::codec::Decode for Backtrace {
    fn decode<D: genlayer_calldata::codec::Deserializer>(
        deserializer: D,
    ) -> Result<Self, genlayer_calldata::codec::DecodeError> {
        Vec::<Frame>::decode(deserializer).map(|frames| Self { frames })
    }
}

impl genlayer_calldata::codec::Decode for StorageDelta {
    fn decode<D: genlayer_calldata::codec::Deserializer>(
        deserializer: D,
    ) -> Result<Self, genlayer_calldata::codec::DecodeError> {
        let parts = Vec::<Bytes>::decode(deserializer)?;
        let [key, value] = <[Bytes; 2]>::try_from(parts).map_err(|_| {
            genlayer_calldata::codec::DecodeError::Unexpected("storage delta must be a pair")
        })?;
        let key = <[u8; 36]>::try_from(key.as_ref()).map_err(|_| {
            genlayer_calldata::codec::DecodeError::Unexpected("storage delta key must be 36 bytes")
        })?;
        Ok(Self::new(key, value.to_vec()))
    }
}

#[derive(Debug, Clone, Copy, Default, genlayer_calldata::Encode, genlayer_calldata::Decode)]
pub struct BucketsConsumed {
    pub storage: primitive_types::U256,
    pub message_receipt: primitive_types::U256,
    pub nondet_output: primitive_types::U256,
    pub message_fee: primitive_types::U256,
    pub event: primitive_types::U256,
}

/// Fingerprint of a run's outcome that a caller folds into its own sub-VM
/// hashes.
///
/// It commits to the outcome only, never to how the run was hosted, so an
/// in-process child and one executed by another executor over the nested
/// protocol are indistinguishable to consensus. Every line compiles this one
/// definition; changing it changes every line at once, which is the point.
pub fn small_hash<D, H>(
    kind: ResultCode,
    data: &D,
    subvm_hashes: &[u8],
    wasm_store_hashes: &H,
) -> [u8; 32]
where
    for<'a> D:
        genlayer_calldata::codec::Encode<&'a mut HashWriter, Error = std::convert::Infallible>,
    for<'a> H:
        genlayer_calldata::codec::Encode<&'a mut HashWriter, Error = std::convert::Infallible>,
{
    use sha3::Digest as _;

    fn encode<D, H>(
        enc: &mut genlayer_calldata::Encoder<&mut HashWriter>,
        kind: ResultCode,
        data: &D,
        subvm_hashes: &[u8],
        wasm_store_hashes: &H,
    ) -> Result<(), std::convert::Infallible>
    where
        for<'a> D:
            genlayer_calldata::codec::Encode<&'a mut HashWriter, Error = std::convert::Infallible>,
        for<'a> H:
            genlayer_calldata::codec::Encode<&'a mut HashWriter, Error = std::convert::Infallible>,
    {
        enc.start_map(4)?;

        enc.push_map_k("kind")?;
        enc.push_str(match kind {
            ResultCode::Return => "Return",
            ResultCode::UserError => "UserError",
            // Neither an internal error nor fatality is part of the outcome
            // itself: the first is never handed to a caller as a result, and the
            // second only says the caller may not catch it. The value folded is
            // the same `vm_error` either way, so a timeout hashes alike whether
            // or not the line that served it can raise fatality.
            ResultCode::VmError | ResultCode::InternalError | ResultCode::FatalVmError => "VMError",
        })?;

        enc.push_map_k("result")?;
        genlayer_calldata::codec::Encode::encode(data, enc)?;

        enc.push_map_k("subvm_hashes")?;
        enc.push_bytes(subvm_hashes)?;

        enc.push_map_k("wasm_store_hashes")?;
        genlayer_calldata::codec::Encode::encode(wasm_store_hashes, enc)?;

        Ok(())
    }

    let mut hasher = HashWriter(sha3::Sha3_256::new());
    let mut enc = genlayer_calldata::Encoder::new(&mut hasher);
    match encode(&mut enc, kind, data, subvm_hashes, wasm_store_hashes) {
        Ok(()) => {}
        Err(e) => match e {},
    }
    hasher.0.finalize().into()
}

/// Sink that feeds an encoded value straight into the [`small_hash`] digest,
/// so the hash never materializes the encoding.
pub struct HashWriter(sha3::Sha3_256);

impl genlayer_calldata::Writer for &mut HashWriter {
    type Error = std::convert::Infallible;

    fn write_all(&mut self, data: &[u8]) -> Result<(), Self::Error> {
        sha3::Digest::update(&mut self.0, data);
        Ok(())
    }
}

#[derive(Debug, Clone, genlayer_calldata::Encode, genlayer_calldata::Decode)]
pub struct ReportedResult {
    pub execution_hash: bytes::Bytes,
    /// See [`small_hash`]: the route-invariant part of the outcome, which is
    /// what a caller in another executor folds.
    pub small_hash: bytes::Bytes,

    /// `FatalVmError` is legal only while transporting a nested result; a
    /// top-level report must publish the same payload as `VmError`
    pub kind: ResultCode,
    pub data: genlayer_calldata::unparsed::Maybe<genlayer_calldata::Value>,
    pub backtrace: Option<Backtrace>,
    pub wasm_store_hashes: WasmStoreHashes,
    pub storage_deltas: Vec<StorageDelta>,

    pub emissions: Vec<ExecutionEmission>,

    pub nondet_disagreement: Option<u32>,
    pub nondet_results: Vec<bytes::Bytes>,

    pub data_fees_remaining: Vec<primitive_types::U256>,
    pub data_fees_consumed: BucketsConsumed,

    pub llm_consumed_gen_wei: primitive_types::U256,
}
