use std::collections::BTreeMap;

use serde_derive::{Deserialize, Serialize};

use genlayer_calldata::codec::{
    Decode, DecodeError as CalldataError, Deserializer, Encode, MapAccess, SeqAccess, Visitor,
};
use genlayer_calldata::{Encoder, Writer};

pub trait Web {
    fn get_webpage(
        &self,
        config: String,
        url: String,
    ) -> tokio::task::JoinHandle<anyhow::Result<Box<[u8]>>>;
}

pub mod abi_stub;
pub mod domain;
pub use abi_stub::*;
pub use domain::*;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(untagged)]
pub enum GenericValue {
    Null,
    Bool(bool),
    Str(String),
    Bytes(#[serde(with = "serde_bytes")] Vec<u8>),
    Number(f64),
    Map(BTreeMap<String, GenericValue>),
    Array(Vec<GenericValue>),
}

impl From<String> for GenericValue {
    fn from(value: String) -> Self {
        GenericValue::Str(value)
    }
}

impl From<i32> for GenericValue {
    fn from(value: i32) -> Self {
        GenericValue::Number(value as f64)
    }
}

impl From<u16> for GenericValue {
    fn from(value: u16) -> Self {
        GenericValue::Number(value as f64)
    }
}

impl From<f64> for GenericValue {
    fn from(value: f64) -> Self {
        GenericValue::Number(value)
    }
}

impl From<u32> for GenericValue {
    fn from(value: u32) -> Self {
        GenericValue::Number(value as f64)
    }
}

impl From<bool> for GenericValue {
    fn from(value: bool) -> Self {
        GenericValue::Bool(value)
    }
}

impl From<Vec<u8>> for GenericValue {
    fn from(value: Vec<u8>) -> Self {
        GenericValue::Bytes(value)
    }
}

impl From<serde_json::Value> for GenericValue {
    fn from(value: serde_json::Value) -> Self {
        match value {
            serde_json::Value::Null => GenericValue::Null,
            serde_json::Value::Bool(x) => GenericValue::Bool(x),
            serde_json::Value::Number(number) => GenericValue::Number(number.as_f64().unwrap()),
            serde_json::Value::String(s) => GenericValue::Str(s),
            serde_json::Value::Array(values) => {
                GenericValue::Array(values.into_iter().map(Into::into).collect())
            }
            serde_json::Value::Object(map) => GenericValue::Map(BTreeMap::from_iter(
                map.into_iter().map(|(k, v)| (k, v.into())),
            )),
        }
    }
}

impl GenericValue {
    pub fn as_str(&self) -> Option<&str> {
        match self {
            GenericValue::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_num(&self) -> Option<f64> {
        match self {
            GenericValue::Number(s) => Some(*s),
            _ => None,
        }
    }
}

// ── Manual Encode for GenericValue ──────────────────────────────────

impl<W: Writer> Encode<W> for GenericValue {
    type Error = W::Error;

    fn encode(&self, enc: &mut Encoder<W>) -> std::result::Result<(), Self::Error> {
        match self {
            GenericValue::Null => enc.push_null(),
            GenericValue::Bool(v) => enc.push_bool(*v),
            GenericValue::Str(s) => enc.push_str(s),
            GenericValue::Bytes(b) => enc.push_bytes(b),
            GenericValue::Number(f) => {
                let i = *f as i64;
                assert!(
                    i as f64 == *f,
                    "GenericValue::Number({f}) is not an exact integer, cannot encode as bigint"
                );
                enc.push_i64(i)
            }
            GenericValue::Map(map) => {
                enc.start_map(map.len() as u64)?;
                for (k, v) in map {
                    enc.push_map_k(k)?;
                    v.encode(enc)?;
                }
                Ok(())
            }
            GenericValue::Array(arr) => {
                enc.start_array(arr.len() as u64)?;
                for item in arr {
                    item.encode(enc)?;
                }
                Ok(())
            }
        }
    }
}

// ── Manual Decode for GenericValue ──────────────────────────────────

impl Decode for GenericValue {
    fn decode<D: Deserializer>(deserializer: D) -> std::result::Result<Self, CalldataError> {
        struct V;
        impl Visitor for V {
            type Value = GenericValue;

            fn visit_null(self) -> std::result::Result<GenericValue, CalldataError> {
                Ok(GenericValue::Null)
            }

            fn visit_bool(self, value: bool) -> std::result::Result<GenericValue, CalldataError> {
                Ok(GenericValue::Bool(value))
            }

            fn visit_str(self, value: &str) -> std::result::Result<GenericValue, CalldataError> {
                Ok(GenericValue::Str(value.to_owned()))
            }

            fn visit_bytes(self, value: &[u8]) -> std::result::Result<GenericValue, CalldataError> {
                Ok(GenericValue::Bytes(value.to_vec()))
            }

            fn visit_bigint(
                self,
                value: &num_bigint::BigInt,
            ) -> std::result::Result<GenericValue, CalldataError> {
                use num_traits::ToPrimitive;
                let f = value.to_f64().ok_or_else(|| {
                    CalldataError::Custom(format!("bigint {value} cannot be represented as f64"))
                })?;
                Ok(GenericValue::Number(f))
            }

            fn visit_bigint_owned(
                self,
                value: num_bigint::BigInt,
            ) -> std::result::Result<GenericValue, CalldataError> {
                self.visit_bigint(&value)
            }

            fn visit_i64(self, value: i64) -> std::result::Result<GenericValue, CalldataError> {
                Ok(GenericValue::Number(value as f64))
            }

            fn visit_u64(self, value: u64) -> std::result::Result<GenericValue, CalldataError> {
                Ok(GenericValue::Number(value as f64))
            }

            fn visit_seq<A: SeqAccess>(
                self,
                len: u64,
                mut seq: A,
            ) -> std::result::Result<GenericValue, CalldataError> {
                let mut result = Vec::with_capacity(len as usize);
                while let Some(elem) = seq.next_element::<GenericValue>()? {
                    result.push(elem);
                }
                Ok(GenericValue::Array(result))
            }

            fn visit_map<A: MapAccess>(
                self,
                _len: u64,
                mut map: A,
            ) -> std::result::Result<GenericValue, CalldataError> {
                let mut result = BTreeMap::new();
                while let Some((key, value)) = map.next_element::<GenericValue>()? {
                    result.insert(key.to_owned(), value);
                }
                Ok(GenericValue::Map(result))
            }
        }
        deserializer.deserialize(V)
    }
}

// ── Encode / Decode for Result<T> ─────────────────────────────────

#[derive(Clone, Deserialize, Serialize)]
pub enum Result<T> {
    Ok(T),
    UserError(GenericValue),
    FatalError(String),
}

// Externally tagged enum: {"Ok": T} | {"UserError": GenericValue} | {"FatalError": String}
impl<W: Writer, T: Encode<W, Error = W::Error>> Encode<W> for Result<T> {
    type Error = W::Error;

    fn encode(&self, enc: &mut Encoder<W>) -> std::result::Result<(), Self::Error> {
        enc.start_map(1)?;
        match self {
            Result::Ok(v) => {
                enc.push_map_k("Ok")?;
                v.encode(enc)?;
            }
            Result::UserError(v) => {
                enc.push_map_k("UserError")?;
                v.encode(enc)?;
            }
            Result::FatalError(s) => {
                enc.push_map_k("FatalError")?;
                enc.push_str(s)?;
            }
        }
        Ok(())
    }
}

// Externally tagged enum: "Ok" or {"Ok": T}, {"UserError": GenericValue}, {"FatalError": String}
impl<T: Decode> Decode for Result<T> {
    fn decode<D: Deserializer>(deserializer: D) -> std::result::Result<Self, CalldataError> {
        struct V<T>(std::marker::PhantomData<T>);
        impl<T: Decode> Visitor for V<T> {
            type Value = Result<T>;

            fn visit_map<A: MapAccess>(
                self,
                _len: u64,
                mut map: A,
            ) -> std::result::Result<Result<T>, CalldataError> {
                let (key, val) = map.next_element::<genlayer_calldata::Value>()?.ok_or(
                    CalldataError::Custom("expected single-key map for Result enum variant".into()),
                )?;
                match key {
                    "FatalError" => {
                        let inner =
                            String::decode(genlayer_calldata::codec::ValueDeserializer(val))?;
                        Ok(Result::FatalError(inner))
                    }
                    "Ok" => {
                        let inner = T::decode(genlayer_calldata::codec::ValueDeserializer(val))?;
                        Ok(Result::Ok(inner))
                    }
                    "UserError" => {
                        let inner =
                            GenericValue::decode(genlayer_calldata::codec::ValueDeserializer(val))?;
                        Ok(Result::UserError(inner))
                    }
                    _ => Err(CalldataError::Custom(format!(
                        "unknown variant `{key}`, expected one of: FatalError, Ok, UserError"
                    ))),
                }
            }
        }
        deserializer.deserialize(V(std::marker::PhantomData))
    }
}

pub mod llm {
    use std::collections::BTreeMap;

    use genlayer_calldata::{Decode, Encode};
    use serde_derive::{Deserialize, Serialize};

    /// Output format for LLM prompt responses.
    #[derive(Clone, Deserialize, Serialize, Encode, Decode, Copy, PartialEq, Eq, Debug)]
    pub enum OutputFormat {
        #[serde(rename = "text")]
        #[calldata(rename = "text")]
        Text,
        #[serde(rename = "json")]
        #[calldata(rename = "json")]
        JSON,
    }

    fn default_text() -> OutputFormat {
        OutputFormat::Text
    }

    /// Payload for ExecPrompt operations.
    #[derive(Clone, PartialEq, Serialize, Deserialize, Encode, Decode, Debug)]
    pub struct PromptPayload {
        #[serde(default = "default_text")]
        #[calldata(default = default_text)]
        pub response_format: OutputFormat,
        pub prompt: String,
        pub images: Vec<bytes::Bytes>,
    }

    #[derive(Clone, PartialEq, Serialize, Deserialize, Encode, Decode, Debug)]
    pub struct PromptEqComparativePayload {
        pub leader_answer: String,
        pub validator_answer: String,
        pub principle: String,
    }

    #[derive(Clone, PartialEq, Serialize, Deserialize, Encode, Decode, Debug)]
    pub struct PromptEqNonComparativeValidatorPayload {
        pub task: String,
        pub criteria: String,
        pub input: String,
        pub output: String,
    }

    #[derive(Clone, PartialEq, Serialize, Deserialize, Encode, Decode, Debug)]
    pub struct PromptEqNonComparativeLeaderPayload {
        pub task: String,
        pub criteria: String,
        pub input: String,
    }

    /// Payload for ExecPromptTemplate operations.
    #[derive(Clone, PartialEq, Serialize, Deserialize, Debug)]
    #[serde(tag = "template")]
    pub enum PromptTemplatePayload {
        EqComparative(PromptEqComparativePayload),
        EqNonComparativeValidator(PromptEqNonComparativeValidatorPayload),
        EqNonComparativeLeader(PromptEqNonComparativeLeaderPayload),
    }

    const TEMPLATE_TAG: &str = "template";

    impl<W: genlayer_calldata::Writer> genlayer_calldata::codec::Encode<W> for PromptTemplatePayload {
        type Error = W::Error;

        fn encode(&self, enc: &mut genlayer_calldata::Encoder<W>) -> Result<(), W::Error> {
            match self {
                // fields sorted: leader_answer, principle, template, validator_answer
                PromptTemplatePayload::EqComparative(p) => {
                    enc.start_map(4)?;
                    enc.push_map_k("leader_answer")?;
                    p.leader_answer.encode(enc)?;
                    enc.push_map_k("principle")?;
                    p.principle.encode(enc)?;
                    enc.push_map_k(TEMPLATE_TAG)?;
                    enc.push_str("EqComparative")?;
                    enc.push_map_k("validator_answer")?;
                    p.validator_answer.encode(enc)?;
                }
                // fields sorted: criteria, input, output, task, template
                PromptTemplatePayload::EqNonComparativeValidator(p) => {
                    enc.start_map(5)?;
                    enc.push_map_k("criteria")?;
                    p.criteria.encode(enc)?;
                    enc.push_map_k("input")?;
                    p.input.encode(enc)?;
                    enc.push_map_k("output")?;
                    p.output.encode(enc)?;
                    enc.push_map_k("task")?;
                    p.task.encode(enc)?;
                    enc.push_map_k(TEMPLATE_TAG)?;
                    enc.push_str("EqNonComparativeValidator")?;
                }
                // fields sorted: criteria, input, task, template
                PromptTemplatePayload::EqNonComparativeLeader(p) => {
                    enc.start_map(4)?;
                    enc.push_map_k("criteria")?;
                    p.criteria.encode(enc)?;
                    enc.push_map_k("input")?;
                    p.input.encode(enc)?;
                    enc.push_map_k("task")?;
                    p.task.encode(enc)?;
                    enc.push_map_k(TEMPLATE_TAG)?;
                    enc.push_str("EqNonComparativeLeader")?;
                }
            }
            Ok(())
        }
    }

    impl genlayer_calldata::codec::Decode for PromptTemplatePayload {
        fn decode<D: genlayer_calldata::codec::Deserializer>(
            deserializer: D,
        ) -> Result<Self, genlayer_calldata::codec::DecodeError> {
            use genlayer_calldata::codec::{DecodeError, MapAccess, ValueDeserializer, Visitor};
            use genlayer_calldata::Value;
            use std::collections::BTreeMap;

            struct V;
            impl Visitor for V {
                type Value = PromptTemplatePayload;

                fn visit_map<A: MapAccess>(
                    self,
                    _len: u64,
                    mut map: A,
                ) -> Result<PromptTemplatePayload, DecodeError> {
                    let mut entries = BTreeMap::<String, Value>::new();
                    while let Some((key, val)) = map.next_element::<Value>()? {
                        entries.insert(key.to_owned(), val);
                    }

                    let tag_val = entries
                        .remove(TEMPLATE_TAG)
                        .ok_or(DecodeError::FieldMissing(TEMPLATE_TAG))?;
                    let Value::Str(tag_str) = tag_val else {
                        return Err(DecodeError::Unexpected("expected string for template tag"));
                    };

                    match tag_str.as_str() {
                        "EqComparative" => {
                            let leader_answer = entries
                                .remove("leader_answer")
                                .ok_or(DecodeError::FieldMissing("leader_answer"))?;
                            let validator_answer = entries
                                .remove("validator_answer")
                                .ok_or(DecodeError::FieldMissing("validator_answer"))?;
                            let principle = entries
                                .remove("principle")
                                .ok_or(DecodeError::FieldMissing("principle"))?;
                            Ok(PromptTemplatePayload::EqComparative(
                                PromptEqComparativePayload {
                                    leader_answer: genlayer_calldata::codec::Decode::decode(
                                        ValueDeserializer(leader_answer),
                                    )?,
                                    validator_answer: genlayer_calldata::codec::Decode::decode(
                                        ValueDeserializer(validator_answer),
                                    )?,
                                    principle: genlayer_calldata::codec::Decode::decode(
                                        ValueDeserializer(principle),
                                    )?,
                                },
                            ))
                        }
                        "EqNonComparativeValidator" => {
                            let task = entries
                                .remove("task")
                                .ok_or(DecodeError::FieldMissing("task"))?;
                            let criteria = entries
                                .remove("criteria")
                                .ok_or(DecodeError::FieldMissing("criteria"))?;
                            let input = entries
                                .remove("input")
                                .ok_or(DecodeError::FieldMissing("input"))?;
                            let output = entries
                                .remove("output")
                                .ok_or(DecodeError::FieldMissing("output"))?;
                            Ok(PromptTemplatePayload::EqNonComparativeValidator(
                                PromptEqNonComparativeValidatorPayload {
                                    task: genlayer_calldata::codec::Decode::decode(
                                        ValueDeserializer(task),
                                    )?,
                                    criteria: genlayer_calldata::codec::Decode::decode(
                                        ValueDeserializer(criteria),
                                    )?,
                                    input: genlayer_calldata::codec::Decode::decode(
                                        ValueDeserializer(input),
                                    )?,
                                    output: genlayer_calldata::codec::Decode::decode(
                                        ValueDeserializer(output),
                                    )?,
                                },
                            ))
                        }
                        "EqNonComparativeLeader" => {
                            let task = entries
                                .remove("task")
                                .ok_or(DecodeError::FieldMissing("task"))?;
                            let criteria = entries
                                .remove("criteria")
                                .ok_or(DecodeError::FieldMissing("criteria"))?;
                            let input = entries
                                .remove("input")
                                .ok_or(DecodeError::FieldMissing("input"))?;
                            Ok(PromptTemplatePayload::EqNonComparativeLeader(
                                PromptEqNonComparativeLeaderPayload {
                                    task: genlayer_calldata::codec::Decode::decode(
                                        ValueDeserializer(task),
                                    )?,
                                    criteria: genlayer_calldata::codec::Decode::decode(
                                        ValueDeserializer(criteria),
                                    )?,
                                    input: genlayer_calldata::codec::Decode::decode(
                                        ValueDeserializer(input),
                                    )?,
                                },
                            ))
                        }
                        other => Err(DecodeError::UnknownVariant {
                            got: other.to_owned(),
                            expected:
                                "EqComparative, EqNonComparativeValidator, EqNonComparativeLeader",
                        }),
                    }
                }
            }

            deserializer.deserialize(V)
        }
    }

    #[derive(Serialize, Deserialize, genlayer_calldata::Encode, genlayer_calldata::Decode)]
    pub enum Message {
        Prompt {
            payload: PromptPayload,
            remaining_fuel_as_gen: primitive_types::U256,
        },
        PromptTemplate {
            payload: PromptTemplatePayload,
            remaining_fuel_as_gen: primitive_types::U256,
        },
    }

    #[derive(
        Serialize,
        Deserialize,
        Debug,
        PartialEq,
        Eq,
        genlayer_calldata::Encode,
        genlayer_calldata::Decode,
    )]
    #[serde(untagged)]
    #[calldata(untagged)]
    pub enum PromptAnswerData {
        Text(String),
        Object(BTreeMap<String, genlayer_calldata::Value>),
        Bool(bool),
    }

    #[derive(
        Serialize,
        Deserialize,
        Debug,
        PartialEq,
        Eq,
        genlayer_calldata::Encode,
        genlayer_calldata::Decode,
    )]
    pub struct PromptAnswer {
        pub data: PromptAnswerData,
        pub consumed_gen: primitive_types::U256,
    }

    impl PromptAnswer {
        pub fn map_text(&mut self, f: impl FnOnce(&mut String)) {
            if let PromptAnswerData::Text(t) = &mut self.data {
                f(t)
            }
        }
    }
}

pub mod web {
    use genlayer_calldata::{Decode, Encode};
    use serde_derive::{Deserialize, Serialize};
    use std::collections::BTreeMap;

    /// Render mode for WebRender operations.
    #[derive(Clone, PartialEq, Serialize, Deserialize, Encode, Decode, Debug)]
    pub enum RenderMode {
        #[serde(rename = "text")]
        #[calldata(rename = "text")]
        Text,
        #[serde(rename = "html")]
        #[calldata(rename = "html")]
        HTML,
        #[serde(rename = "screenshot")]
        #[calldata(rename = "screenshot")]
        Screenshot,
    }

    /// Duration to wait after page load before capturing content.
    #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
    pub enum WaitAfterLoaded {
        Seconds(u64),
        Millis(u64),
    }

    impl WaitAfterLoaded {
        pub fn as_secs_f64(&self) -> f64 {
            match self {
                WaitAfterLoaded::Seconds(s) => *s as f64,
                WaitAfterLoaded::Millis(ms) => *ms as f64 / 1000.0,
            }
        }
    }

    struct WaitAfterLoadedVisitor;

    impl serde::de::Visitor<'_> for WaitAfterLoadedVisitor {
        type Value = WaitAfterLoaded;

        fn expecting(&self, formatter: &mut std::fmt::Formatter) -> std::fmt::Result {
            formatter.write_str("expected string | null")
        }

        fn visit_none<E>(self) -> std::result::Result<Self::Value, E>
        where
            E: serde::de::Error,
        {
            Ok(WaitAfterLoaded::Millis(0))
        }

        fn visit_str<E>(self, value: &str) -> std::result::Result<Self::Value, E>
        where
            E: serde::de::Error,
        {
            if let Some(ms_str) = value.strip_suffix("ms") {
                let millis = ms_str.parse::<u64>().map_err(E::custom)?;
                Ok(WaitAfterLoaded::Millis(millis))
            } else if let Some(secs_str) = value.strip_suffix("s") {
                let seconds = secs_str.parse::<u64>().map_err(E::custom)?;
                Ok(WaitAfterLoaded::Seconds(seconds))
            } else {
                Err(E::invalid_value(
                    serde::de::Unexpected::Str(value),
                    &"expected a string ending with 's' or 'ms'",
                ))
            }
        }
    }

    impl<'de> serde::Deserialize<'de> for WaitAfterLoaded {
        fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
        where
            D: serde::Deserializer<'de>,
        {
            deserializer.deserialize_str(WaitAfterLoadedVisitor)
        }
    }

    impl serde::Serialize for WaitAfterLoaded {
        fn serialize<S>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error>
        where
            S: serde::Serializer,
        {
            match self {
                WaitAfterLoaded::Seconds(v) => {
                    let as_str = format!("{}s", v);
                    serializer.serialize_str(&as_str)
                }
                WaitAfterLoaded::Millis(v) => {
                    let as_str = format!("{}ms", v);
                    serializer.serialize_str(&as_str)
                }
            }
        }
    }

    fn encode_wait_after_loaded<W: genlayer_calldata::Writer>(
        val: &WaitAfterLoaded,
        enc: &mut genlayer_calldata::Encoder<W>,
    ) -> Result<(), W::Error> {
        let s = match val {
            WaitAfterLoaded::Seconds(v) => format!("{v}s"),
            WaitAfterLoaded::Millis(v) => format!("{v}ms"),
        };
        enc.push_str(&s)
    }

    fn decode_wait_after_loaded(
        val: genlayer_calldata::Value,
    ) -> Result<WaitAfterLoaded, genlayer_calldata::codec::DecodeError> {
        let genlayer_calldata::Value::Str(s) = val else {
            return Err(genlayer_calldata::codec::DecodeError::Unexpected(
                "expected string",
            ));
        };
        if let Some(ms_str) = s.strip_suffix("ms") {
            let millis = ms_str
                .parse::<u64>()
                .map_err(|e| genlayer_calldata::codec::DecodeError::UserError(Box::new(e)))?;
            Ok(WaitAfterLoaded::Millis(millis))
        } else if let Some(secs_str) = s.strip_suffix("s") {
            let seconds = secs_str
                .parse::<u64>()
                .map_err(|e| genlayer_calldata::codec::DecodeError::UserError(Box::new(e)))?;
            Ok(WaitAfterLoaded::Seconds(seconds))
        } else {
            Err(genlayer_calldata::codec::DecodeError::Unexpected(
                "expected string ending with 's' or 'ms'",
            ))
        }
    }

    fn default_none<T>() -> Option<T> {
        None
    }

    fn default_false() -> bool {
        false
    }

    /// Payload for WebRender operations.
    #[derive(Clone, PartialEq, Serialize, Deserialize, Encode, Decode, Debug)]
    pub struct RenderPayload {
        pub mode: RenderMode,
        pub url: String,
        #[calldata(
            serialize_with = encode_wait_after_loaded,
            deserialize_with = decode_wait_after_loaded
        )]
        pub wait_after_loaded: WaitAfterLoaded,
    }

    /// HTTP request method for WebRequest operations.
    #[derive(Clone, PartialEq, Debug, Serialize, Deserialize, Encode, Decode)]
    pub enum RequestMethod {
        GET,
        POST,
        HEAD,
        PUT,
        DELETE,
        OPTIONS,
        PATCH,
    }

    /// HTTP response from WebRequest or WebRender operations.
    #[derive(Clone, PartialEq, Debug, Serialize, Deserialize, Encode, Decode)]
    pub struct Response {
        pub status: u16,
        pub headers: BTreeMap<String, bytes::Bytes>,

        #[serde(with = "serde_bytes")]
        #[calldata(
            serialize_with = genlayer_calldata::codec::as_bytes::serialize,
            deserialize_with = genlayer_calldata::codec::as_bytes::deserialize,
        )]
        pub body: Vec<u8>,
    }

    /// Payload for WebRequest operations.
    #[derive(Clone, PartialEq, Serialize, Deserialize, Encode, Decode, Debug)]
    pub struct RequestPayload {
        pub method: RequestMethod,
        pub url: String,
        pub headers: BTreeMap<String, bytes::Bytes>,

        #[serde(with = "serde_bytes", default = "default_none")]
        #[calldata(
            default = default_none,
            serialize_with = genlayer_calldata::codec::as_bytes::serialize,
            deserialize_with = genlayer_calldata::codec::as_bytes::deserialize,
        )]
        pub body: Option<Vec<u8>>,
        #[serde(default = "default_false")]
        #[calldata(default = default_false)]
        pub sign: bool,
    }

    #[derive(Serialize, Deserialize, genlayer_calldata::Encode, genlayer_calldata::Decode)]
    pub enum Message {
        Render(RenderPayload, u32),
        Request(RequestPayload, u32),
    }

    #[derive(Serialize, Deserialize, genlayer_calldata::Encode, genlayer_calldata::Decode)]
    pub enum RenderAnswer {
        #[serde(rename = "response")]
        #[calldata(rename = "response")]
        Response(Response),
        #[serde(rename = "text")]
        #[calldata(rename = "text")]
        Text(String),
        #[serde(rename = "image", with = "serde_bytes")]
        #[calldata(
            rename = "image",
            serialize_with = ::genlayer_calldata::codec::as_bytes::serialize,
            deserialize_with = ::genlayer_calldata::codec::as_bytes::deserialize,
        )]
        Image(Vec<u8>),
    }
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct HostData {
    pub node_address: String,
    pub tx_id: String,
    #[serde(flatten)]
    pub rest: serde_json::Map<String, serde_json::Value>,
}

#[derive(
    Clone,
    Debug,
    PartialEq,
    Eq,
    Hash,
    serde::Serialize,
    serde::Deserialize,
    Copy,
    PartialOrd,
    Ord,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
)]
pub struct GenVMId(pub u64);

impl std::fmt::Display for GenVMId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

#[derive(
    Clone,
    Copy,
    Debug,
    PartialEq,
    Eq,
    Serialize,
    Deserialize,
    genlayer_calldata::Encode,
    genlayer_calldata::Decode,
)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Leader,
    Validator,
}

#[derive(Debug, Serialize, Deserialize, genlayer_calldata::Encode, genlayer_calldata::Decode)]
pub struct GenVMHello {
    pub genvm_id: GenVMId,
    pub role: Role,
    #[calldata(deserialize_with = decode_host_data)]
    pub host_data: HostData,
    #[serde(default)]
    pub gas_data: std::collections::BTreeMap<String, String>,
    pub initial_time_units_allocation: u32,
}

/// Decode a `serde_json::Value` from a calldata `Value`.
fn calldata_to_json(
    val: genlayer_calldata::Value,
) -> std::result::Result<serde_json::Value, CalldataError> {
    match val {
        genlayer_calldata::Value::Null => Ok(serde_json::Value::Null),
        genlayer_calldata::Value::Bool(b) => Ok(serde_json::Value::Bool(b)),
        genlayer_calldata::Value::Number(n) => {
            use num_traits::ToPrimitive;
            let i = n.to_i64().ok_or_else(|| {
                CalldataError::Custom(format!("bigint {n} cannot be represented as i64"))
            })?;
            Ok(serde_json::Value::Number(serde_json::Number::from(i)))
        }
        genlayer_calldata::Value::Str(s) => Ok(serde_json::Value::String(s)),
        genlayer_calldata::Value::Bytes(_) => Err(CalldataError::Custom(
            "cannot convert calldata bytes to serde_json::Value".into(),
        )),
        genlayer_calldata::Value::Address(_) => Err(CalldataError::Custom(
            "cannot convert calldata address to serde_json::Value".into(),
        )),
        genlayer_calldata::Value::Array(arr) => {
            let mut out = Vec::with_capacity(arr.len());
            for v in arr {
                out.push(calldata_to_json(v)?);
            }
            Ok(serde_json::Value::Array(out))
        }
        genlayer_calldata::Value::Map(map) => {
            let mut out = serde_json::Map::new();
            for (k, v) in map {
                out.insert(k, calldata_to_json(v)?);
            }
            Ok(serde_json::Value::Object(out))
        }
    }
}

/// Custom deserializer for HostData from calldata. HostData is encoded as a map
/// with known fields `node_address` and `tx_id`; everything else goes into `rest`.
fn decode_host_data(val: genlayer_calldata::Value) -> std::result::Result<HostData, CalldataError> {
    let genlayer_calldata::Value::Map(mut map) = val else {
        return Err(CalldataError::Unexpected("expected map for HostData"));
    };

    let node_address = map
        .remove("node_address")
        .ok_or(CalldataError::Custom("missing field `node_address`".into()))
        .and_then(|v| match v {
            genlayer_calldata::Value::Str(s) => Ok(s),
            _ => Err(CalldataError::Unexpected(
                "expected string for node_address",
            )),
        })?;

    let tx_id = map
        .remove("tx_id")
        .ok_or(CalldataError::Custom("missing field `tx_id`".into()))
        .and_then(|v| match v {
            genlayer_calldata::Value::Str(s) => Ok(s),
            _ => Err(CalldataError::Unexpected("expected string for tx_id")),
        })?;

    let mut rest = serde_json::Map::new();
    for (k, v) in map {
        rest.insert(k, calldata_to_json(v)?);
    }

    Ok(HostData {
        node_address,
        tx_id,
        rest,
    })
}

// ── Manual Encode for HostData (flattened serde_json fields) ───────

/// Helper: encode a serde_json::Value into calldata format.
fn encode_json_value<W: Writer>(
    value: &serde_json::Value,
    enc: &mut Encoder<W>,
) -> std::result::Result<(), W::Error> {
    match value {
        serde_json::Value::Null => enc.push_null(),
        serde_json::Value::Bool(b) => enc.push_bool(*b),
        serde_json::Value::Number(n) => {
            // JSON numbers: try i64, then u64, then error (no f64 in calldata)
            if let Some(i) = n.as_i64() {
                enc.push_i64(i)
            } else if let Some(u) = n.as_u64() {
                enc.push_u64(u)
            } else {
                panic!(
                    "serde_json::Number({n}) cannot be represented as integer for calldata encoding"
                );
            }
        }
        serde_json::Value::String(s) => enc.push_str(s),
        serde_json::Value::Array(arr) => {
            enc.start_array(arr.len() as u64)?;
            for item in arr {
                encode_json_value(item, enc)?;
            }
            Ok(())
        }
        serde_json::Value::Object(map) => {
            // serde_json::Map iteration order may not be sorted; collect and sort
            let mut entries: Vec<_> = map.iter().collect();
            entries.sort_by_key(|(k, _)| k.as_str());
            enc.start_map(entries.len() as u64)?;
            for (k, v) in entries {
                enc.push_map_k(k)?;
                encode_json_value(v, enc)?;
            }
            Ok(())
        }
    }
}

impl<W: Writer> Encode<W> for HostData {
    type Error = W::Error;

    fn encode(&self, enc: &mut Encoder<W>) -> std::result::Result<(), Self::Error> {
        // Collect all keys: the two named fields + all keys from `rest`.
        // Must be emitted in sorted order for the calldata map format.
        let mut entries: Vec<(&str, Option<&serde_json::Value>)> = Vec::new();

        // Sentinel: None means it's one of the named fields
        entries.push(("node_address", None));
        entries.push(("tx_id", None));

        for (k, v) in &self.rest {
            entries.push((k.as_str(), Some(v)));
        }

        entries.sort_by_key(|(k, _)| *k);

        enc.start_map(entries.len() as u64)?;
        for (key, json_val) in &entries {
            enc.push_map_k(key)?;
            match (key, json_val) {
                (&"node_address", None) => enc.push_str(&self.node_address)?,
                (&"tx_id", None) => enc.push_str(&self.tx_id)?,
                (_, Some(v)) => encode_json_value(v, enc)?,
                _ => unreachable!(),
            }
        }
        Ok(())
    }
}
