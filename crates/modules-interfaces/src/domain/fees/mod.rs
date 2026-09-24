use primitive_types::U256;

use crate::On;

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
pub struct InternalMessageParams {
    pub leader_timeunits_allocation: U256,
    pub validator_timeunits_allocation: U256,
    pub execution_budget_per_round: U256,
    /// Per-round rotation allocations; `rotations[0]` is the initial round, the
    /// rest are appeal rounds. Must be non-empty.
    ///
    /// The fee evaluator derives `appeal_rounds = rotations.len() - 1`.
    pub rotations: Vec<U256>,
    /// Per-time-unit GEN price cap locked at activation (consensus CON-549,
    /// v0.6-dev). The chain charges at this cap as the funding multiplier and
    /// cancels the tx if the global price exceeds it; `MessagePayments` requires
    /// it to be non-zero for internal messages.
    pub max_price_gen_per_time_unit: U256,
    /// Max gas price applied to the storage-fee component (v0.6-dev). Revert
    /// guard in `_calculateRoundFees`; must be non-zero for internal messages.
    pub storage_fee_max_gas_price: U256,
    /// Max gas price applied to the receipt-fee component (v0.6-dev). Revert
    /// guard in `_calculateRoundFees`; must be non-zero for internal messages.
    pub receipt_fee_max_gas_price: U256,
}

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
pub struct ExternalMessageParams {
    pub gas_limit: U256,
    pub max_gas_price: U256,
}

#[derive(
    Debug,
    Clone,
    serde::Serialize,
    serde::Deserialize,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
)]
pub enum MessageAllocationNodeParams {
    Internal(std::sync::Arc<InternalMessageParams>),
    External(ExternalMessageParams),
}

/// One allocation available to messages emitted by this execution.
#[derive(
    Debug,
    Clone,
    serde::Serialize,
    serde::Deserialize,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
)]
pub struct MessageAllocationNode {
    /// Target contract address; `None` means wildcard (any recipient).
    pub recipient: Option<genlayer_calldata::Address>,
    /// `None` means any call key; the node converts the chain's wildcard sentinel.
    pub call_key: Option<crate::abi_stub::CallKey>,
    /// Available allowance; zero is exhausted, `None` is uncapped.
    pub budget: Option<U256>,
    pub on: On,
    pub fee_params: MessageAllocationNodeParams,
    /// Sum of the direct descendants' allocation budgets, funded per emission.
    pub children_budget: U256,
    /// Host-encoded matched subtree, including any proof required by the chain.
    pub subtree: bytes::Bytes,
}

impl MessageAllocationNode {
    pub fn matches_internal(
        &self,
        on: On,
        recipient: genlayer_calldata::Address,
        call_key: crate::abi_stub::CallKey,
    ) -> Option<std::sync::Arc<InternalMessageParams>> {
        let MessageAllocationNodeParams::Internal(params) = &self.fee_params else {
            return None;
        };
        (on == self.on
            && self.recipient.is_none_or(|r| r == recipient)
            && self.call_key.is_none_or(|ck| ck == call_key))
        .then(|| params.clone())
    }

    pub fn matches_external(
        &self,
        recipient: genlayer_calldata::Address,
        call_key: crate::abi_stub::CallKey,
    ) -> Option<ExternalMessageParams> {
        let MessageAllocationNodeParams::External(params) = &self.fee_params else {
            return None;
        };
        (self.recipient.is_none_or(|r| r == recipient)
            && self.call_key.is_none_or(|ck| ck == call_key))
        .then_some(*params)
    }
}
