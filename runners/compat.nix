# Runner-to-executor compatibility policy. Swap this file out to change it.
#
# `compatible runnerVersion executorVersion` answers: may an executor pinned at
# `executorVersion` ship a runner that was introduced in `runnerVersion`?
#
# General rule: a runner is compatible with every executor at or after its own
# version — i.e. each executor gets "all runners up to and including my version".
# This models forward replay: a newer executor must still run the exact runner
# an old contract pinned.
#
# Exception — frozen legacy lines: the v0.2.x line is a backport of the legacy
# v0.2 ABI (string-only user errors, empty-string method key, non-int floats
# encoded as strings, gl_call user-error -> rollback). Its runners are built
# against that old ABI and must NOT roll up into newer executors, which speak the
# new ABI — otherwise v0.3 would silently accept and run v0.2.16 runners. So v0.2
# runners serve ONLY v0.2 executors; every newer line still rolls forward as
# usual.
runnerVersion: executorVersion:
let
  isLegacy = v: builtins.match "v?0\\.2(\\..*)?" v != null;
in
if isLegacy runnerVersion then
  isLegacy executorVersion
else
  builtins.compareVersions runnerVersion executorVersion <= 0
