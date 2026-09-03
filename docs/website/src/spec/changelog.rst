Changelog
=========

User-observable changes to the specification, grouped by executor line (see
:doc:`01-core-architecture/03-versioning`). Internal refactors are omitted;
see the repository history for those

.. _gvm-changelog-v0-3:

v0.3
----

Breaking
~~~~~~~~

#. The pre-finalization state is spelled *decided* everywhere it is named.
   :ref:`gvm-def-enum-storage-view` reads ``latest_finalized`` (1) and
   ``latest_decided`` (2) instead of ``latest_final`` and ``latest_non_final``,
   the ``on`` field of the ``EmitInternalMessage`` and
   ``EmitInternalDeployMessage`` ``gl_call``
   payloads (:doc:`02-execution-environment/03-wasi_genlayer_sdk/02-gl_call`)
   takes ``"decided"`` instead of ``"accepted"``, and a ``chain:`` runner id
   selects it with ``d`` rather than ``a`` (see
   :ref:`gvm-def-chain-runner-state`). Numeric enum values are unchanged; none
   of the old spellings is accepted
#. A ``chain:`` runner id resolved while a contract is being deployed
   canonicalizes to ``i``, which previously spelled the deploy state as ``d``
#. The ``When`` action's ``cond`` field spells the non-deterministic mode
   ``!det``; the previous ``nondet`` spelling is rejected, with no
   back-compat alias. See the ``When`` action in
   :doc:`02-execution-environment/04-runners`
#. ``runner.json`` rejects unknown top-level and nested fields; a runner that
   relied on an extra field being silently ignored fails to load. The single
   top-level ``$schema`` string annotation is still accepted. See
   :doc:`appendix/runner-schema` and the action definitions in
   :doc:`02-execution-environment/04-runners`
#. ZIP-packaged :term:`runners <Runner>` are accepted under narrower rules; an
   archive that previously loaded despite violating one of them now fails.
   See the "ZIP Archive" layout in :doc:`02-execution-environment/04-runners`:

   #. Compression must read ``stored`` in both the central directory and the
      entry's local header
   #. A stored entry's compressed and uncompressed sizes must be equal
   #. Every entry's CRC-32 is validated against its declared value
   #. A directory entry must carry no contents; it is skipped rather than
      added to the file list
   #. Entry names are validated at parse time: no empty name, no leading
      ``/``, no backslash, no empty, ``.`` or ``..`` path component, no
      trailing ``/`` on a file
   #. Entries sharing a name resolve to the last of them

Changed
~~~~~~~

#. A missing or malformed runner archive/comment header is now reported as
   :ref:`gvm-def-str-trie-value-vm-error-invalid-contract-runner-absent` or
   :ref:`gvm-def-str-trie-value-vm-error-invalid-contract-runner-malformed`
   respectively, instead of the former ``invalid_contract absent_runner_comment``
   and ``invalid_contract malformed_runner`` codes. A malformed runner archive
   also now reports the precise
   :ref:`gvm-def-str-trie-value-vm-error-invalid-contract-runner-malformed`
   code where it previously surfaced a bare ``invalid_contract``
#. A :ref:`gvm-def-gl-call-run-nondet` block starts on a budget seeded with its
   caller's remaining RAM instead of a fresh 4 GiB one, so it can no longer use
   more RAM than the deterministic caller had left. See
   :doc:`03-vm/03-ram-limiting`
#. Emitted messages, events, and leader nondeterministic outputs consume RAM for
   their retained representations. Their charges, like storage write charges,
   remain until execution ends and transfer to a caller that adopts a sandbox
   child's retained data. See :doc:`03-vm/03-ram-limiting`
#. A resource error raised while a module is being instantiated keeps its own
   code — e.g. exhausting the memory budget there reports
   :ref:`gvm-def-str-trie-value-vm-error-out-of-memory-wasm-memory` — instead of
   being reported as a bare ``invalid_contract``
