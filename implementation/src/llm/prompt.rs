use std::sync::Arc;

use base64::Engine;
use serde::{Deserialize, Serialize};

use crate::common::{ModuleError, ModuleResult};

pub use super::merge::MergeStrategy;

/// NOTE: when changing fields, also update Prompt in modules/install/lib/genvm-lua/lib-llm.lua
#[derive(Serialize, Deserialize)]
pub struct Internal {
    pub system_message: Option<String>,
    pub user_message: String,
    pub temperature: f32,
    pub images: Vec<Arc<ImageLua>>,

    pub max_tokens: u32,
    pub use_max_completion_tokens: bool,
    pub seed: Option<u64>,

    #[serde(default)]
    pub extra: serde_json::Map<String, serde_json::Value>,
    #[serde(default)]
    pub extra_merge_strategy: MergeStrategy,

    #[serde(default)]
    pub timeout: Option<crate::common::Timeout>,
}

impl Internal {
    pub fn apply_timeout(&self, request: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        if let Some(timeout) = self.timeout {
            request.timeout(timeout.to_duration())
        } else {
            request
        }
    }

    /// Upper bound on the provider response body we buffer in memory. Budgets
    /// ~16 bytes per requested output token (~4 bytes/char * ~4 chars/token)
    /// plus a 16 MiB base, hard-capped so a large `max_tokens` cannot blow up
    /// the limit (`u32::MAX` would otherwise allow ~64 GiB).
    pub fn response_body_limit(&self) -> usize {
        const BASE_LIMIT: usize = 16 * 1024 * 1024;
        const MAX_LIMIT: usize = 32 * 1024 * 1024;

        (self.max_tokens as usize)
            .saturating_mul(16)
            .saturating_add(BASE_LIMIT)
            .min(MAX_LIMIT)
    }
}

/// Matches the largest side the strictest provider (Anthropic) accepts
pub const IMAGE_MAX_SIDE: u32 = 8000;
/// Fits an 8-bit RGB JPEG at [`IMAGE_MAX_SIDE`] squared. Independent of the
/// executor's `EXEC_PROMPT_MIN_SPACE`, which reserves contract memory instead
const IMAGE_MAX_ALLOC: u64 = 256 * 1024 * 1024;

#[derive(Serialize, Deserialize, Clone, Copy)]
pub enum ImageType {
    PNG,
    JPG,
}

impl ImageType {
    pub fn sniff(data: &[u8]) -> Option<ImageType> {
        if data.starts_with(&[0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) {
            Some(ImageType::PNG)
        } else if data.starts_with(&[0xFF, 0xD8, 0xFF, 0xE0]) {
            Some(ImageType::JPG)
        } else {
            None
        }
    }

    /// Whether `data` fully decodes as `self` within [`IMAGE_MAX_SIDE`]; it is
    /// CPU-bound, so call it off the async runtime
    pub fn decodes(self, data: &[u8]) -> bool {
        match self {
            Self::JPG => jpeg_decodes(data),
            Self::PNG => png_decodes(data).unwrap_or(false),
        }
    }

    pub fn media_type(self) -> &'static str {
        match self {
            Self::JPG => "image/jpeg",
            Self::PNG => "image/png",
        }
    }
}

/// Holds the whole bitmap in memory, bounded by [`IMAGE_MAX_ALLOC`]
fn jpeg_decodes(data: &[u8]) -> bool {
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(IMAGE_MAX_SIDE);
    limits.max_image_height = Some(IMAGE_MAX_SIDE);
    limits.max_alloc = Some(IMAGE_MAX_ALLOC);

    let mut reader =
        image::ImageReader::with_format(std::io::Cursor::new(data), image::ImageFormat::Jpeg);
    reader.limits(limits);
    reader.decode().is_ok()
}

/// Streams rows to the end of the file, so memory stays a few rows whatever the
/// declared size
fn png_decodes(data: &[u8]) -> Result<bool, png::DecodingError> {
    let mut decoder = png::Decoder::new(std::io::Cursor::new(data));
    // expanding is what rejects a palette image that has no `PLTE`
    decoder.set_transformations(png::Transformations::EXPAND);

    let header = decoder.read_header_info()?;
    if header.width > IMAGE_MAX_SIDE || header.height > IMAGE_MAX_SIDE {
        return Ok(false);
    }

    let mut reader = decoder.read_info()?;
    while reader.next_row()?.is_some() {}
    reader.finish()?;
    Ok(true)
}

#[derive(Serialize, Deserialize)]
pub struct ImageLua(#[serde(with = "serde_bytes")] pub Vec<u8>);

impl ImageLua {
    pub fn as_base64(&self) -> String {
        base64::prelude::BASE64_STANDARD.encode(&self.0)
    }

    pub fn kind_or_error(&self) -> ModuleResult<ImageType> {
        ImageType::sniff(&self.0).ok_or_else(|| {
            ModuleError {
                causes: vec!["INVALID_IMAGE".into()],
                fatal: true,
                ctx: std::collections::BTreeMap::new(),
            }
            .into()
        })
    }
}

#[derive(Serialize, Deserialize, Debug)]
pub enum ExtendedOutputFormat {
    #[serde(rename = "text")]
    Text,
    #[serde(rename = "json")]
    JSON,
    #[serde(rename = "bool")]
    Bool,
}
