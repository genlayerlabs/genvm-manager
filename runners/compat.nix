# Runner-to-executor compatibility policy. Swap this file out to change it.
#
# `compatible runnerVersion executorVersion` answers: may an executor pinned at
# `executorVersion` ship a runner that was introduced in `runnerVersion`?
#
# For now: a runner is compatible with every executor at or after its own
# version — i.e. each executor gets "all runners up to and including my version".
runnerVersion: executorVersion: builtins.compareVersions runnerVersion executorVersion <= 0
