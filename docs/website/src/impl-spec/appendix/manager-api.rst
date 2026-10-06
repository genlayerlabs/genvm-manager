Manager API
===========

The GenVM Manager exposes an HTTP server for administration (status, modules,
permits, log level, manifest, LLM checks, error descriptions, contract version
detection) and a framed socket for driving executions
(:doc:`manager-socket`).

The execution endpoints below -- ``POST /genvm/run``, ``GET /genvm/{id}``,
``DELETE /genvm/{id}`` -- are **deprecated**: they remain for one release
train as a thin adapter over the same execution core the socket uses, then
get removed. New host integrations MUST use the socket protocol.

Two endpoints below are tightly coupled with the runner manifest (the per-line
available-runners listing, generated from ``runners-versions.json``, published on
each executor line's docs sub-site):

- ``POST /contract/detect-version`` returns the public-ABI ``specified_major`` that the
  node MUST persist into the contract's root-slot ``major`` field (see
  :doc:`/impl-spec/02-vm/02-version-management` and
  :doc:`/spec/04-contract-interface/03-storage`).
- ``POST /manifest/reload`` re-reads the runner manifest from disk; until reload, runner
  versions added to the JSON are not visible to the manager.

.. openapi:: manager-api.yaml

.. include:: manager-api.yaml
   :literal:
