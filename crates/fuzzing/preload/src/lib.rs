//! `LD_PRELOAD` shim that makes the OS randomness syscalls deterministic.
//!
//! Loaded into AFL targets only, via `AFL_PRELOAD`. Without it every process
//! seeds its hashers from real entropy, so a saved crash need not replay and
//! coverage differs between runs.

use core::ffi::{c_int, c_uint, c_void};
use std::cell::Cell;

thread_local! {
    /// Number of `u64` words handed out by this thread.
    static WORDS: Cell<u64> = const { Cell::new(0) };
}

fn next_word() -> u64 {
    // splitmix64 over the call counter
    let mut z = WORDS.with(|words| {
        let current = words.get();
        words.set(current.wrapping_add(1));
        current.wrapping_add(0x9e37_79b9_7f4a_7c15)
    });
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    z ^ (z >> 31)
}

/// # Safety
/// `buffer` must be writable for `length` bytes, or `length` must be zero.
unsafe fn fill(buffer: *mut c_void, length: usize) {
    if length == 0 {
        return;
    }
    assert!(!buffer.is_null(), "genvm-fuzz-preload: null buffer");

    let mut written = 0;
    while written < length {
        let word = next_word().to_le_bytes();
        let chunk = word.len().min(length - written);
        std::ptr::copy_nonoverlapping(word.as_ptr(), (buffer as *mut u8).add(written), chunk);
        written += chunk;
    }
}

/// # Safety
/// See [`fill`]. Matches the libc signature, flags are ignored: every flag
/// combination is satisfiable when the entropy is fake.
#[no_mangle]
pub unsafe extern "C" fn getrandom(buffer: *mut c_void, length: usize, _flags: c_uint) -> isize {
    fill(buffer, length);
    length as isize
}

/// # Safety
/// See [`fill`]. Unlike libc this accepts any `length`; the 256 byte cap exists
/// to bound how much real entropy one call may drain.
#[no_mangle]
pub unsafe extern "C" fn getentropy(buffer: *mut c_void, length: usize) -> c_int {
    fill(buffer, length);
    0
}

#[cfg(test)]
mod tests {
    use super::next_word;

    #[test]
    fn threads_have_the_same_sequence() {
        let threads: Vec<_> = (0..4)
            .map(|_| std::thread::spawn(|| [next_word(), next_word()]))
            .collect();
        let sequences: Vec<_> = threads
            .into_iter()
            .map(|thread| thread.join().unwrap())
            .collect();

        assert!(sequences.windows(2).all(|pair| pair[0] == pair[1]));
    }
}
