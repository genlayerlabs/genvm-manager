# Fuzzing

Rust fuzz targets run under AFL (`cargo-afl`):

```bash
genvm-tool test run --filter-tag "$(cat tests/presets/rust-fuzz.txt)"   # rust & fuzz
```

Python fuzz targets exist but their collection is currently disabled in
`.genvm-tool.py`. Some integration cases also carry the `fuzz` tag.

Options: `--fuzz-timeout N` (seconds per run, default 30),
`--fuzz-update-corpus` (write back interesting inputs).

## Host preparation

AFL needs these before a session (crashes must land as files, CPU frequency
must be stable, and the fuzzer must be able to ptrace):

```bash
echo core | sudo tee /proc/sys/kernel/core_pattern
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```
