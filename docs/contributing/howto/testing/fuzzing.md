# Fuzzing

Rust and Python fuzz targets run under AFL. A Rust target is any `fuzz/*.rs`
next to a tracked `Cargo.toml`; a Python target is any `fuzz/src/*.py` in a
collected Python project:

```bash
genvm-tool test run --filter-tag "$(cat tests/presets/rust-fuzz.txt)"     # rust & fuzz
genvm-tool test run --filter-tag "$(cat tests/presets/python-fuzz.txt)"   # python & needs-fuzz
```

A corpus is committed next to its target: `fuzz/inputs-<target>` for Rust,
`fuzz/inputs/<target>` for Python. Beside it, `<corpus>-curated` holds seeds
written by hand — see [Curated Seeds](#curated-seeds)

A generated entry is named `gvm32(sha3_224(input))` and stored under the first
two characters of that name, `<corpus>/<n0>/<n1>/<name>`, so a corpus of
thousands of entries stays a tree of small directories. Reading takes an entry
wherever it sits, so entries an older run left directly in the corpus dir are
still used; a corpus update rewrites the whole dir and shards them

`needs-fuzz` is the tag to select on: it means the case drives AFL and wants the
host prepared as below, whereas the plain `fuzz` tag some integration cases carry
only says the case is randomized. The CI cell that runs the AFL cases is `other`

Python targets are collected on x86_64 Linux only — the pinned python-afl and
AFL++ are packaged for that platform alone

The shared AFL test plugin owns the process fleet for both languages. Options:
`--fuzz-timeout D` (per run, a duration such as `30s` or `5m`, default `30s`),
`--fuzz-concurrent N` (targets side by side, default: cores minus two),
`--fuzz-jobs N` (processes per target, default: the cores `--fuzz-concurrent`
leaves over, so one), and `--fuzz-update-corpus` (write interesting inputs back)

A fleet is N instances sharing one output dir; they only exchange corpora every
`AFL_SYNC_TIME` (20 minutes, halved for the `-M` instance), so below that it is N
independent searches — still worth it, since every instance's crashes are
collected. That is why the default spends cores across targets rather than within
one: a second instance of a target re-searches what the first one covers, while
another target covers code neither reaches

AFL's live UI needs the terminal to itself, so it is only shown when a single
target runs at a time (`--fuzz-concurrent 1`). Otherwise every instance writes to
`<output dir>/<instance>.log` and the case reports the closing `afl-whatsup`
summary

`--fuzz-concurrent` sizes the fleet; what physically bounds overlap is the case
pool, `--max-concurrent` (default: the core count). A fuzz case holds one permit
of it per fuzzer process it starts

A crash is copied into the target's generated corpus (`fuzz/inputs-<target>`,
named as a corpus entry) as soon as it is reported, so every later run replays
it — the next run wipes the crash dir it was saved in. Commit it along with the
fix; put it in `<corpus>-curated` instead if a corpus update must never drop it

Startup replays every committed entry, and AFL skips the ones it cannot use
rather than aborting the run: a `-t` ceiling of 5 s (it auto-scales below that)
keeps a seed that grew slower than AFL's 1 s default from taking the target down.
A seed that *crashes* is skipped by the same switch, so the case reads it back out
of the instance logs and fails with the entry's name under `crashing_seeds`

Findings of both languages live under `build/test-artifacts/fuzz/<project>/`.
Reruns resume from that dir (`AFL_AUTORESUME`) and therefore ignore `-i`: remove
it to pick up a corpus that changed. Corpus updates `cmin` the fleet's queues
and replace the committed corpus with what survives, laid out as above — so the
first update of a corpus moves every file it keeps

## Curated Seeds

`cmin` judges an input by the edges it reaches, so a seed written to pin a shape
down — the one input covering a format's corner — is dropped as soon as some
havoc'd blob happens to cover the same edges. Put such an input in
`<corpus>-curated` instead: `fuzz/inputs-<target>-curated` for Rust,
`fuzz/inputs/<target>-curated` for Python

That dir is read-only to the tooling. Every run stages it together with the
generated corpus into the `-seeds` dir it hands to `-i`, and neither
`--fuzz-update-corpus` nor `fuzz-corpus` ever writes or deletes anything in it.
Name the files for humans — `deeply-nested-object`, not a content hash; the
staging renames them on the way in, and a corpus update drops the copy `cmin`
kept of a curated seed rather than committing it twice. A `README.txt` there is
not a seed, so the dir can say what its inputs are for

A curated seed still has to buy something: `fuzz-corpus` counts its coverage as
already-covered, so a harvested blob reaching nothing beyond it is not added

## Seeding a Corpus From a Test Run

Random bytes are almost never a zip or a calldata frame, so a target with a
committed corpus of a dozen entries spends its budget rediscovering the reject
path. The integration suite produces real calldata, contract code and leader
results on every run; `fuzz-corpus` harvests them from the artifacts dir into
`fuzz/inputs-<target>`, named by content hash:

```bash
genvm-tool test run --filter-tag integration    # produce the artifacts
genvm-tool fuzz-corpus --dry-run                # what it would add, and why not
genvm-tool fuzz-corpus
```

Each candidate is kept only if `afl-showmap` says it reaches an edge the corpus
does not already cover, so the committed corpus grows by seeds that buy
something. It also drops what a corpus should not carry: a blob above
`--max-size`, or one embedding a path of the machine that produced it. The check
needs the targets built; `--no-verify` skips it

A harvest only adds; run the targets once with `--fuzz-update-corpus` afterwards
to `cmin` the union back down, and check the repo delta before committing

Nothing is harvested for a `-structured` target or its op-sequence kin: they take
a serialized value rather than a format, which a run does not produce. Seed those
by driving their mutator, as below

## Structured Targets

A target whose input is a value rather than a byte format comes in two halves.
`<name>-raw` takes the bytes verbatim and is what covers the parser. Next to it,
`<name>-structured` takes a [`postcard`] encoding of the value, and a mutator
crate at `fuzz/mutators/<name>/` mutates that value structurally — AFL loads it
through `AFL_CUSTOM_MUTATOR_LIBRARY`, which the test plugin sets whenever such a
crate exists

The input type lives in its own file under `fuzz/shared/`, pulled into both the
target and the mutator crate with `#[path = ...] mod`. Keep that file free of
anything the executor owns: the mutator crate is a small `cdylib` and must not
have to link `genvm` to build

AFL's own mutations are deliberately left on. Handing every mutation to the
custom mutator (`AFL_CUSTOM_MUTATOR_ONLY`) measures worse — a havoc'd buffer that
fails to decode costs one cheap early return, while the ones that do decode reach
values the structural mutator never proposes. `GENVM_FUZZ_MUTATOR_ONLY=1` turns
it on to compare

### Seeding One

A structural mutator only mutates a value it already has, so a `-structured`
target needs a non-empty corpus before it explores anything. Generate one by
driving the built mutator library, which makes every seed valid by construction:

```python
import ctypes, pathlib
from genvm_tool.misc import fuzz_input_name, fuzz_input_path

lib = ctypes.CDLL('<target-dir>/debug/libfuzz_mutator_<name>.so')
lib.afl_custom_init.restype = ctypes.c_void_p
lib.afl_custom_init.argtypes = [ctypes.c_void_p, ctypes.c_uint]
lib.afl_custom_fuzz.restype = ctypes.c_size_t
lib.afl_custom_fuzz.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_char_p), ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t]

state, current = lib.afl_custom_init(None, 0), b'\x00'   # the default value
for _ in range(60):
    out = ctypes.c_char_p()
    n = lib.afl_custom_fuzz(state, current, len(current), ctypes.byref(out), None, 0, 4096)
    current = ctypes.string_at(out, n)
    path = fuzz_input_path(pathlib.Path('fuzz/inputs-<name>'), fuzz_input_name(current))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(current)
```

Two things to check in the result. Every variant of the input type should appear
across the seeds — a mutator that cannot construct one leaves that variant's code
unreachable, and seeds generated from it will not cover it either. And the type's
own `serde` impls are not always a corpus codec: `calldata::Value` describes
itself to the format (a null is a unit, a number is an `i64` when it fits), which
`postcard` cannot read back, so it has a separate `Corpus` representation

[`postcard`]: https://docs.rs/postcard

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
