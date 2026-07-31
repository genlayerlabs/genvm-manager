# Fuzzing

Rust fuzz targets run under AFL (`cargo-afl`); a target is any `fuzz/*.rs` next
to a tracked `Cargo.toml`:

```bash
genvm-tool test run --filter-tag "$(cat tests/presets/rust-fuzz.txt)"   # rust & fuzz
```

Python fuzz targets exist, but their collection is currently disabled in
`.genvm-tool.py`. Some integration cases carry the `fuzz` tag too

Options: `--fuzz-timeout N` (seconds per run, default 30) and
`--fuzz-update-corpus` (write interesting inputs back)

## Host Preparation

AFL needs crashes to land as files, a stable CPU frequency, and permission to
ptrace:

```bash
echo core | sudo tee /proc/sys/kernel/core_pattern
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
cat /proc/sys/kernel/yama/ptrace_scope   # note it down, to restore afterwards
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

The last one is machine-wide and lasts until reboot: with `ptrace_scope` at `0`
any process of yours can attach to any other one of yours, browser and ssh-agent
included. Fuzz on a throwaway machine or a container, and put the noted value
back (`echo <value> | sudo tee /proc/sys/kernel/yama/ptrace_scope`) when you are
done
