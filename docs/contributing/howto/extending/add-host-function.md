# Adding a Host Function

1. `executors/<line>.x/executor/codegen/data/host-fns.json` — add the method id,
   then `ninja -C build codegen` ([genvm-tool.md](../genvm-tool.md)). That
   regenerates the line's `executor/crates/common/src/host_fns.rs` and the
   manager's `tests/runner/origin/host_fns.py`
2. `executors/<line>.x/executor/src/host/mod.rs` — wire the executor-side client
3. `tests/runner/origin/base_host.py` — handle the new case in the read loop and
   add the method to the `IHost` protocol. `origin/` is mirrored into the node,
   so keep it self-contained
4. `tests/runner/gvm_extra/mock_host.py` — add the test implementation
5. Update the node and simulator host implementations, in their own repos
