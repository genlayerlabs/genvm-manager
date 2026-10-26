pub mod common;
pub mod manager;
pub mod scripting;

pub mod llm;
pub mod web;

pub use scripting::filters;

/// code point of the trailing `\uXXXX` escape, if the string ends in one
fn trailing_unicode_escape(s: &str) -> Option<u32> {
    let start = s.len().checked_sub(6)?;
    let b = s.as_bytes();

    if b[start] != b'\\' || b[start + 1] != b'u' {
        return None;
    }

    let hex = &s[start + 2..];
    if !hex.bytes().all(|c| c.is_ascii_hexdigit()) {
        return None;
    }

    let slashes = s[..start].bytes().rev().take_while(|&c| c == b'\\').count();
    if slashes % 2 == 1 {
        return None;
    }

    u32::from_str_radix(hex, 16).ok()
}

pub fn complete_json(partial: &str) -> String {
    let mut result = partial.trim_end().to_string();

    // each frame is (closing char, whether the current object member already has a colon)
    let mut stack: Vec<(char, bool)> = Vec::new();
    let mut in_string = false;
    let mut escape_next = false;

    for ch in partial.chars() {
        if escape_next {
            escape_next = false;
            continue;
        }

        match ch {
            '\\' if in_string => escape_next = true,
            '"' => in_string = !in_string,
            '{' if !in_string => stack.push(('}', false)),
            '[' if !in_string => stack.push((']', false)),
            '}' if !in_string => {
                stack.pop();
            }
            ']' if !in_string => {
                stack.pop();
            }
            ':' if !in_string => {
                if let Some(frame) = stack.last_mut() {
                    frame.1 = true;
                }
            }
            ',' if !in_string => {
                if let Some(frame @ ('}', _)) = stack.last_mut() {
                    frame.1 = false;
                }
            }
            _ => {}
        }
    }

    if in_string {
        if escape_next {
            result.pop();
        }

        let mut s_rev = result.chars().rev();

        let mut hex_count = 0;
        let mut had_u = false;
        for _i in 0..4 {
            if let Some(ch) = s_rev.next() {
                if ch.is_ascii_hexdigit() {
                    hex_count += 1;
                } else {
                    if ch == 'u' || ch == 'U' {
                        had_u = true;
                    }

                    break;
                }
            } else {
                break;
            }
        }

        if had_u {
            let mut slashes = 0;

            while s_rev.next() == Some('\\') {
                slashes += 1;
            }

            if slashes % 2 == 1 {
                result.truncate(result.len() - 2 - hex_count);
            }
        }

        // a surrogate pair cut in half leaves a lone surrogate, which JSON rejects
        while let Some(cp) = trailing_unicode_escape(&result) {
            let paired = (0xdc00..0xe000).contains(&cp)
                && trailing_unicode_escape(&result[..result.len() - 6])
                    .is_some_and(|hi| (0xd800..0xdc00).contains(&hi));

            if paired || !(0xd800..0xe000).contains(&cp) {
                break;
            }

            result.truncate(result.len() - 6);
        }

        result.push('"');
    }

    let trimmed_len = result.trim_end().len();
    result.truncate(trimmed_len);

    if result.ends_with('{') {
        // nothing
    } else if result.ends_with(':') {
        result.push_str("null");
    } else if result.ends_with('-')
        || result.ends_with('.')
        || result.ends_with('+')
        || ((result.ends_with('e') || result.ends_with('E'))
            && result.chars().rev().nth(1).unwrap_or('a').is_ascii_digit())
    {
        result.push('0');
    } else if result.ends_with(',') {
        result.pop();
    } else if stack.last() == Some(&('}', false)) && !result.ends_with('{') {
        result.push_str(":null");
    } else {
        const CONSTS: &[&str] = &["true", "false", "null"];

        for &c in CONSTS {
            for i in 1..c.len() {
                if result.ends_with(&c[..c.len() - i]) {
                    result.push_str(&c[c.len() - i..]);
                    break;
                }
            }
            if result.ends_with(c) {
                break;
            }
        }
    }

    while let Some((closing, _)) = stack.pop() {
        result.push(closing);
    }

    if result.is_empty() {
        result.push_str("{}");
    }

    result
}
