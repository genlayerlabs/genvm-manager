# Adding a host function

1. `executors/<line>.x/executor/codegen/data/host-fns.json` — add the method
   id, then regenerate (`ninja -C build codegen`); this updates
   `executor/crates/common/src/host_fns.rs` (per line) and the manager's
   `tests/runner/origin/host_fns.py` — see [genvm-tool.md](../genvm-tool.md).
2. `tests/runner/origin/base_host.py` — handle the new case in the read loop
   and add the method to the `IHost` protocol. NOTE: `origin/` is mirrored
   into the node (`backend/node/genvm/origin/`).
3. `tests/runner/gvm_extra/mock_host.py` — add the test implementation.
4. Update the node (and simulator) host implementations.
