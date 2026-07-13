use bytes::Bytes;
use primitive_types::U256;

pub mod fees;

fn default_record_actions() -> Vec<String> {
    Vec::new()
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
}

#[allow(clippy::enum_variant_names)]
#[derive(Debug, Clone, PartialEq, Eq, genlayer_calldata::Encode)]
#[calldata(tag = "type")]
pub enum ExecutionEmission {
    EthSend {
        address: genlayer_calldata::Address,
        calldata: Bytes,
        value: U256,
        message_fee: U256,
        receipt_fee: U256,

        fee_params: fees::ExternalMessageParams,
    },
    PostMessage {
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
    DeployContract {
        calldata: genlayer_calldata::codec::Maybe<genlayer_calldata::Value>,
        code: Bytes,
        value: U256,
        on: crate::On,
        salt_nonce: U256,
        message_fee: U256,
        receipt_fee: U256,

        fee_params: fees::InternalMessageParams,
        subtree: bytes::Bytes,
        /// Chain `useBalance`; see `ExecutionEmission::PostMessage::use_balance`.
        use_balance: bool,
    },
    EmitEvent {
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

#[derive(Debug, Clone, serde::Serialize, PartialEq, Eq, genlayer_calldata::Encode)]
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

#[derive(Debug, Clone, Copy, Default, genlayer_calldata::Encode)]
pub struct BucketsConsumed {
    pub storage: primitive_types::U256,
    pub message_receipt: primitive_types::U256,
    pub nondet_output: primitive_types::U256,
    pub message_fee: primitive_types::U256,
    pub event: primitive_types::U256,
}

#[derive(Debug, Clone, genlayer_calldata::Encode)]
pub struct ReportedResult {
    pub execution_hash: bytes::Bytes,

    pub kind: ResultCode,
    pub data: genlayer_calldata::unparsed::Maybe<genlayer_calldata::Value>,
    pub backtrace: Option<Backtrace>,
    pub wasm_store_hashes: WasmStoreHashes,
    pub storage_changes: Vec<StorageDelta>,

    pub emissions: Vec<ExecutionEmission>,

    pub nondet_disagreement: Option<u32>,
    pub nondet_results: Vec<bytes::Bytes>,

    pub data_fees_remaining: Vec<primitive_types::U256>,
    pub data_fees_consumed: BucketsConsumed,

    pub llm_consumption: primitive_types::U256,
}
