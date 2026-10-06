pub use mutatis;
pub use postcard;
pub use serde;

mod depth_limit;

/// Corpus files above this are refused rather than decoded.
pub const MAX_CORPUS_BYTES: usize = 8 * 1024;

/// Maximum number of nested containers in a corpus value.
pub const MAX_CORPUS_DEPTH: usize = 128;

/// Decodes a corpus file into the value a target consumes. A file AFL produced
/// by other means than the custom mutator will not decode, hence the `Option`.
pub fn decode<T: serde::de::DeserializeOwned>(bytes: &[u8]) -> Option<T> {
    if bytes.len() > MAX_CORPUS_BYTES {
        return None;
    }
    let mut deserializer = postcard::Deserializer::from_bytes(bytes);
    serde::Deserialize::deserialize(depth_limit::Deserializer::new(
        &mut deserializer,
        MAX_CORPUS_DEPTH,
    ))
    .ok()
}

/// Encodes a value back into the bytes AFL stores. Only fails when `T`'s
/// `Serialize` does, which for a fuzz input is a bug rather than an input error.
pub fn encode<T: serde::Serialize>(value: &T) -> Vec<u8> {
    postcard::to_allocvec(value).expect("fuzz input must serialize")
}

/// Defines the `afl_custom_*` entry points AFL++ dlopens, mutating `$ty`
/// structurally instead of editing its serialized bytes.
///
/// Expand it in a crate with `crate-type = ["cdylib"]`, next to the `#[path]`
/// module that also defines the target's input type:
///
/// ```ignore
/// #[path = "../../shared/leader-result-input.rs"]
/// mod input;
///
/// genvm_fuzzing::mutator!(input::Input);
/// ```
#[macro_export]
macro_rules! mutator {
    ($ty:ty) => {
        struct MutatorState {
            session: $crate::mutatis::Session,
            buf: ::std::vec::Vec<u8>,
            calls: u64,
        }

        /// How many mutations one call stacks.
        ///
        /// A session applies a single mutation, which on its own explores about
        /// as far from a seed as one bit flip does. AFL's own havoc stage owes
        /// much of its reach to stacking, and a structural mutator that does
        /// not stack loses to it.
        const MAX_STACKED: u64 = 8;

        #[no_mangle]
        pub extern "C" fn afl_custom_init(
            _afl: *mut ::core::ffi::c_void,
            seed: u32,
        ) -> *mut ::core::ffi::c_void {
            let state = ::std::boxed::Box::new(MutatorState {
                session: $crate::mutatis::Session::new().seed(seed as u64),
                buf: ::std::vec::Vec::new(),
                calls: 0,
            });
            ::std::boxed::Box::into_raw(state) as *mut ::core::ffi::c_void
        }

        /// # Safety
        /// `data` is what [`afl_custom_init`] returned.
        #[no_mangle]
        pub unsafe extern "C" fn afl_custom_deinit(data: *mut ::core::ffi::c_void) {
            drop(::std::boxed::Box::from_raw(data as *mut MutatorState));
        }

        /// # Safety
        /// `buf` is readable for `buf_size` bytes and `out_buf` is writable, as
        /// the AFL++ custom mutator API specifies. The returned buffer stays
        /// alive until the next call, which is when AFL is done with it.
        #[no_mangle]
        pub unsafe extern "C" fn afl_custom_fuzz(
            data: *mut ::core::ffi::c_void,
            buf: *mut u8,
            buf_size: usize,
            out_buf: *mut *const u8,
            _add_buf: *mut u8,
            _add_buf_size: usize,
            max_size: usize,
        ) -> usize {
            use $crate::mutatis::Mutate as _;

            let state = &mut *(data as *mut MutatorState);
            let input = ::core::slice::from_raw_parts(buf, buf_size);

            // A seed AFL minimized, spliced or havoc'd is no longer a valid
            // encoding; starting over from the default beats emitting garbage.
            let mut value: $ty = $crate::decode(input).unwrap_or_default();

            // splitmix64 over the call counter, so the count varies without a
            // second source of randomness to seed and replay
            state.calls = state.calls.wrapping_add(1);
            let mut z = state.calls.wrapping_mul(0x9e37_79b9_7f4a_7c15);
            z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
            for _ in 0..=(z >> 32) % MAX_STACKED {
                let _ = state.session.mutate(&mut value);
            }

            state.buf = $crate::encode(&value);

            // Truncating would corrupt the encoding, so an oversized mutation
            // is dropped in favour of the input it came from.
            let max_size = max_size.min($crate::MAX_CORPUS_BYTES);
            if state.buf.len() > max_size {
                state.buf.clear();
                state
                    .buf
                    .extend_from_slice(&input[..input.len().min(max_size)]);
            }

            *out_buf = state.buf.as_ptr();
            state.buf.len()
        }
    };
}

#[cfg(test)]
mod tests {
    use super::{decode, encode, MAX_CORPUS_DEPTH};
    use serde::{Deserialize, Serialize};
    use std::collections::BTreeMap;

    #[derive(Debug, Deserialize, PartialEq, Serialize)]
    struct Mixed {
        values: Vec<Option<u16>>,
        named: BTreeMap<String, bool>,
    }

    #[derive(Debug, Deserialize, PartialEq, Serialize)]
    enum Nested {
        More(Box<Nested>),
        End,
    }

    fn nested(depth: usize) -> Nested {
        (0..depth).fold(Nested::End, |inner, _| Nested::More(Box::new(inner)))
    }

    #[test]
    fn decode_preserves_normal_values() {
        let value = Mixed {
            values: vec![Some(42), None],
            named: BTreeMap::from([("answer".to_owned(), true)]),
        };

        assert_eq!(decode(&encode(&value)), Some(value));
    }

    #[test]
    fn decode_accepts_bounded_nesting() {
        let value = nested(MAX_CORPUS_DEPTH - 1);

        assert_eq!(decode(&encode(&value)), Some(value));
    }

    #[test]
    fn decode_rejects_excessive_nesting() {
        let bytes = encode(&nested(MAX_CORPUS_DEPTH));

        assert_eq!(decode::<Nested>(&bytes), None);
    }
}
