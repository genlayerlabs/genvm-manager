# Why Fuzzing Is Set Up This Way

## Fake Entropy

A fuzz target is only useful if a saved crash reproduces. Real entropy breaks
that: `std` seeds tokio's scheduler and every `HashMap` from `getrandom`, so two
runs over one input take different task orders and different bucket layouts, and
an input AFL saved as a crash need not crash again

So fuzz cases — and no other test — run with `crates/fuzzing/preload` in
`AFL_PRELOAD`, a shim whose `getrandom`/`getentropy` hand out a fixed sequence.
AFL turns that into an `LD_PRELOAD` for the target alone, which is why it is a
preload rather than a patch: the seeding happens in `std`, below any code this
repository owns, and interposing the symbol is the only place to stand

`glibc` seeds its allocator from inside the loader, before a preloaded symbol can
win, so that call stays random — it perturbs heap layout only, and AFL already
disables ASLR. The mechanism also assumes a dynamically linked target: a static
or `musl` build would ignore the preload and say nothing, so the entropy would
come back silently rather than as a failure

## No CmpLog

CmpLog logs both operands of every comparison, letting AFL write a magic value
straight into an input instead of guessing it. It is the standard answer for a
parser gated on tags and headers, and it does nothing for us

Our targets are `arbitrary`-driven: input bytes are *interpreted* to construct a
value, not compared against constants, so there is almost nothing for CmpLog to
log. Measured over 60s per side, `genvm-common-encode` found 1791 edges either
way, and `genvm-common-decode` 1782 against 1785 — noise

The cost is not noise. CmpLog is a second instrumentation, and AFL wants it as a
separate binary so the fuzzing loop stays fast, which means building the whole
dependency graph twice. For one crate that came to 1.2G

Worth revisiting for a target that parses raw bytes rather than driving
`arbitrary` — `runners-parse` reads real zip data and was never measured, since
building its second graph pulls in wasmtime
