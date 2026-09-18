// This file is auto-generated. Do not edit!

#![allow(dead_code, clippy::all)]

use serde::{Deserialize, Serialize};

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
    ConsumeTimeFeeGenWei = 1,
    ExternalCall = 2,
    GetBalanceGenWei = 3,
    GetRemainingTimeFeeGenWei = 4,
    NotifyNondetDisagreement = 5,
    ConsumeResult = 6,
    ResolveCallContractExecutor = 7,
    RunNested = 8,
}

impl Methods {
    pub const SIZE: usize = 9;
    pub fn value(self) -> u8 {
        match self {
            Methods::StorageRead => 0,
            Methods::ConsumeTimeFeeGenWei => 1,
            Methods::ExternalCall => 2,
            Methods::GetBalanceGenWei => 3,
            Methods::GetRemainingTimeFeeGenWei => 4,
            Methods::NotifyNondetDisagreement => 5,
            Methods::ConsumeResult => 6,
            Methods::ResolveCallContractExecutor => 7,
            Methods::RunNested => 8,
        }
    }
    pub fn str_snake_case(self) -> &'static str {
        match self {
            Methods::StorageRead => "storage_read",
            Methods::ConsumeTimeFeeGenWei => "consume_time_fee_gen_wei",
            Methods::ExternalCall => "external_call",
            Methods::GetBalanceGenWei => "get_balance_gen_wei",
            Methods::GetRemainingTimeFeeGenWei => "get_remaining_time_fee_gen_wei",
            Methods::NotifyNondetDisagreement => "notify_nondet_disagreement",
            Methods::ConsumeResult => "consume_result",
            Methods::ResolveCallContractExecutor => "resolve_call_contract_executor",
            Methods::RunNested => "run_nested",
        }
    }
}

impl TryFrom<u8> for Methods {
    type Error = ();

    fn try_from(value: u8) -> Result<Self, ()> {
        match value {
            0 => Ok(Methods::StorageRead),
            1 => Ok(Methods::ConsumeTimeFeeGenWei),
            2 => Ok(Methods::ExternalCall),
            3 => Ok(Methods::GetBalanceGenWei),
            4 => Ok(Methods::GetRemainingTimeFeeGenWei),
            5 => Ok(Methods::NotifyNondetDisagreement),
            6 => Ok(Methods::ConsumeResult),
            7 => Ok(Methods::ResolveCallContractExecutor),
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
pub enum ResultCode {
    Return = 0,
    UserError = 1,
    VmError = 2,
    InternalError = 3,
    FatalVmError = 4,
}

impl ResultCode {
    pub const SIZE: usize = 5;
    pub fn value(self) -> u8 {
        match self {
            ResultCode::Return => 0,
            ResultCode::UserError => 1,
            ResultCode::VmError => 2,
            ResultCode::InternalError => 3,
            ResultCode::FatalVmError => 4,
        }
    }
    pub fn str_snake_case(self) -> &'static str {
        match self {
            ResultCode::Return => "return",
            ResultCode::UserError => "user_error",
            ResultCode::VmError => "vm_error",
            ResultCode::InternalError => "internal_error",
            ResultCode::FatalVmError => "fatal_vm_error",
        }
    }
}

impl TryFrom<u8> for ResultCode {
    type Error = ();

    fn try_from(value: u8) -> Result<Self, ()> {
        match value {
            0 => Ok(ResultCode::Return),
            1 => Ok(ResultCode::UserError),
            2 => Ok(ResultCode::VmError),
            3 => Ok(ResultCode::InternalError),
            4 => Ok(ResultCode::FatalVmError),
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
pub const CURRENT_MAJOR: u8 = 0;
pub const CURRENT_MAJOR_STR: &'static str = "v0.0.0";

// EOF
