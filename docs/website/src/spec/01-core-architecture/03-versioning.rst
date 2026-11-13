Executor Versioning
===================

.. _gvm-def-executor-version:

An *executor* is a released :term:`GenVM` build, identified by a
``v<major>.<minor>.<patch>`` version. The incremented component bounds what may
change between two releases.

Patch
-----

A patch release is fully compatible with its predecessor; executors that differ
only in patch are interchangeable for resyncing from genesis.

#. The :ref:`gvm-def-execution-hash` of every transaction remains the same.
#. Deterministic semantics are exactly the same.

Minor
-----

A minor release adds new features and is backwards-compatible.

#. The :ref:`gvm-def-execution-hash` of a transaction may change.
#. New :term:`Runner`\s may be added; each MUST be gated by a
   :ref:`fork <gvm-def-fork>` of its own, so executors of the previous minor
   agree with it until the fork activates. Existing runners MUST NOT be
   removed.
#. New ``gl_call`` methods and new parameters of existing methods may be added.
#. Old code continues to work, unless it deliberately tried to be
   future-incompatible (e.g. by observing additions permitted by rule 3).

Major
-----

A major release makes almost no promises. Executing code of two different
majors within the same VM is impossible.

If a ``v2.x`` executor is ever released, messages and calls across majors will
be supported by delegating execution to a ``v1.x`` executor.

.. _gvm-def-fork:

Forks
-----

A *fork* is a named switch that a network activates at a time of its own
choosing. The *active set* of a transaction is the set of forks whose activation
time on its network is at or before the transaction timestamp. Validators of a
network compute the same active set only if they share its activation times.

#. A name is ``/``-separated segments of ``[a-z0-9._-]``, rooted at the
   component it belongs to, e.g. ``genvm/executor/v0.3``.
#. A fork may require other forks; a network MUST NOT activate a fork before
   any fork it requires. A fork whose name extends another fork's name
   requires it.
#. ``always`` is in every active set and ``never`` is in none; neither can be
   defined as a fork.
#. An activated fork is never withdrawn.

Each executor line is gated by a fork, and a line is selectable *iff* its fork
is active. A :term:`Runner` is gated as described in
:ref:`gvm-def-runner-availability`. A behaviour change inside a line may also
be gated by a fork, so the line keeps its old behaviour until it activates.

Forced Contract Updates
-----------------------

A contract is forcefully updated to the latest compatible executor version:

#. while syncing — to the latest *patch* only (same ``major.minor``);
#. for new transactions — to the latest ``minor.patch`` (same ``major``).

Required Executor Set
---------------------

- Syncing from genesis requires, for every released ``major.minor``, the
  executor with the greatest ``patch``: ``all(major).all(minor).max(patch)``.
- Executing new transactions requires, for every released ``major``, the
  executor with the greatest ``minor.patch``: ``all(major).max(minor).max(patch)``.

.. _gvm-def-vm-error-compat:

VM Error Code Compatibility
---------------------------

A :ref:`VM error code <gvm-def-vm-error-code>` splits at `` # `` into a public
code and an optional detail.

- **Patch**: the entire string, including the detail, is unchanged (follows
  from the :ref:`gvm-def-execution-hash` promise).
- **Minor**: the set of public codes is add-only — new codes may be added and
  an existing code may be extended with new trailing components; codes are
  never renamed, removed, or reordered. The detail may change arbitrarily.
- **Major**: no promise.

The detail is exempt from the registry of predefined codes; its internal
structure is an implementation detail and MUST NOT be relied upon.

Implementation Note
-------------------

For ``major = 0`` the rules above do not fully hold: a minor increment may
carry either major or minor meaning. Release candidates are allowed to iterate
the latest runners and change their hashes.
