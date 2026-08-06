// This file is auto-generated. Do not edit!

#![allow(dead_code, clippy::redundant_static_lifetimes)]

use serde::{Deserialize, Serialize};

use std::borrow::Cow;

#[derive(
    Debug,
    PartialEq,
    Clone,
    Copy,
    Serialize,
    Deserialize,
    ::genlayer_calldata::Encode,
    ::genlayer_calldata::Decode,
)]
#[repr(u8)]
pub enum Methods {
    StorageRead = 0,
    ConsumeFuel = 1,
    EthCall = 2,
    GetBalance = 3,
    RemainingFuelAsGen = 4,
    NotifyNondetDisagreement = 5,
    ConsumeResult = 6,
    ResolveCallcontractExecutor = 7,
    RunNested = 8,
}

impl Methods {
    pub const SIZE: usize = 9;
    pub fn value(self) -> u8 {
        match self {
            Methods::StorageRead => 0,
            Methods::ConsumeFuel => 1,
            Methods::EthCall => 2,
            Methods::GetBalance => 3,
            Methods::RemainingFuelAsGen => 4,
            Methods::NotifyNondetDisagreement => 5,
            Methods::ConsumeResult => 6,
            Methods::ResolveCallcontractExecutor => 7,
            Methods::RunNested => 8,
        }
    }
    pub fn str_snake_case(self) -> &'static str {
        match self {
            Methods::StorageRead => "storage_read",
            Methods::ConsumeFuel => "consume_fuel",
            Methods::EthCall => "eth_call",
            Methods::GetBalance => "get_balance",
            Methods::RemainingFuelAsGen => "remaining_fuel_as_gen",
            Methods::NotifyNondetDisagreement => "notify_nondet_disagreement",
            Methods::ConsumeResult => "consume_result",
            Methods::ResolveCallcontractExecutor => "resolve_callcontract_executor",
            Methods::RunNested => "run_nested",
        }
    }
}

impl TryFrom<u8> for Methods {
    type Error = ();

    fn try_from(value: u8) -> Result<Self, ()> {
        match value {
            0 => Ok(Methods::StorageRead),
            1 => Ok(Methods::ConsumeFuel),
            2 => Ok(Methods::EthCall),
            3 => Ok(Methods::GetBalance),
            4 => Ok(Methods::RemainingFuelAsGen),
            5 => Ok(Methods::NotifyNondetDisagreement),
            6 => Ok(Methods::ConsumeResult),
            7 => Ok(Methods::ResolveCallcontractExecutor),
            8 => Ok(Methods::RunNested),
            _ => Err(()),
        }
    }
}
#[derive(
    Debug,
    PartialEq,
    Clone,
    Copy,
    Serialize,
    Deserialize,
    ::genlayer_calldata::Encode,
    ::genlayer_calldata::Decode,
)]
#[repr(u8)]
pub enum Errors {
    Ok = 0,
    EvmReverted = 1,
    Forbidden = 2,
}

impl Errors {
    pub const SIZE: usize = 3;
    pub fn value(self) -> u8 {
        match self {
            Errors::Ok => 0,
            Errors::EvmReverted => 1,
            Errors::Forbidden => 2,
        }
    }
    pub fn str_snake_case(self) -> &'static str {
        match self {
            Errors::Ok => "ok",
            Errors::EvmReverted => "evm_reverted",
            Errors::Forbidden => "forbidden",
        }
    }
}

impl TryFrom<u8> for Errors {
    type Error = ();

    fn try_from(value: u8) -> Result<Self, ()> {
        match value {
            0 => Ok(Errors::Ok),
            1 => Ok(Errors::EvmReverted),
            2 => Ok(Errors::Forbidden),
            _ => Err(()),
        }
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct VmErrorDetail(pub Cow<'static, str>);

impl From<VmErrorDetail> for String {
    fn from(val: VmErrorDetail) -> String {
        val.0.into()
    }
}
#[rustfmt::skip]
impl VmErrorDetail {
    pub const fn internal() -> Self { Self(Cow::Borrowed("internal")) }
    pub const fn external() -> Self { Self(Cow::Borrowed("external")) }
}

#[rustfmt::skip]
impl VmErrorDetail {
    /// Whether `s` is a well-formed `vm_error_detail` path.
    pub fn is_valid_(s: &str) -> bool {
        if matches!(s,
            "internal" |
            "external"
        ) {
            return true;
        }
        false
    }
}

pub const CURRENT_MAJOR: u8 = 0;
pub const CURRENT_MAJOR_STR: &'static str = "v0.0.0";

// EOF
