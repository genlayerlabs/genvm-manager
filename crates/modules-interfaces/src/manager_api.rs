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
#[repr(u16)]
pub enum Methods {
    Error = 0,
    Hello = 1,
    Event = 2,
    Run = 3,
    Attach = 4,
    Cancel = 5,
    Ack = 6,
    GetArtifact = 7,
}

impl Methods {
    pub const SIZE: usize = 8;
    pub fn value(self) -> u16 {
        match self {
            Methods::Error => 0,
            Methods::Hello => 1,
            Methods::Event => 2,
            Methods::Run => 3,
            Methods::Attach => 4,
            Methods::Cancel => 5,
            Methods::Ack => 6,
            Methods::GetArtifact => 7,
        }
    }
    pub fn str_snake_case(self) -> &'static str {
        match self {
            Methods::Error => "error",
            Methods::Hello => "hello",
            Methods::Event => "event",
            Methods::Run => "run",
            Methods::Attach => "attach",
            Methods::Cancel => "cancel",
            Methods::Ack => "ack",
            Methods::GetArtifact => "get_artifact",
        }
    }
}

impl TryFrom<u16> for Methods {
    type Error = ();

    fn try_from(value: u16) -> Result<Self, ()> {
        match value {
            0 => Ok(Methods::Error),
            1 => Ok(Methods::Hello),
            2 => Ok(Methods::Event),
            3 => Ok(Methods::Run),
            4 => Ok(Methods::Attach),
            5 => Ok(Methods::Cancel),
            6 => Ok(Methods::Ack),
            7 => Ok(Methods::GetArtifact),
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
    Internal = 0,
    MalformedFrame = 1,
    UnknownMethod = 2,
    UnknownId = 3,
    BootIdMismatch = 4,
    BadRequestId = 5,
    NotFinished = 6,
}

impl Errors {
    pub const SIZE: usize = 7;
    pub fn value(self) -> u8 {
        match self {
            Errors::Internal => 0,
            Errors::MalformedFrame => 1,
            Errors::UnknownMethod => 2,
            Errors::UnknownId => 3,
            Errors::BootIdMismatch => 4,
            Errors::BadRequestId => 5,
            Errors::NotFinished => 6,
        }
    }
    pub fn str_snake_case(self) -> &'static str {
        match self {
            Errors::Internal => "internal",
            Errors::MalformedFrame => "malformed_frame",
            Errors::UnknownMethod => "unknown_method",
            Errors::UnknownId => "unknown_id",
            Errors::BootIdMismatch => "boot_id_mismatch",
            Errors::BadRequestId => "bad_request_id",
            Errors::NotFinished => "not_finished",
        }
    }
}

impl TryFrom<u8> for Errors {
    type Error = ();

    fn try_from(value: u8) -> Result<Self, ()> {
        match value {
            0 => Ok(Errors::Internal),
            1 => Ok(Errors::MalformedFrame),
            2 => Ok(Errors::UnknownMethod),
            3 => Ok(Errors::UnknownId),
            4 => Ok(Errors::BootIdMismatch),
            5 => Ok(Errors::BadRequestId),
            6 => Ok(Errors::NotFinished),
            _ => Err(()),
        }
    }
}
pub const CURRENT_MAJOR: u32 = 0;

// EOF
