Consensus Mechanics
===================

This page documents how the GenVM implementation realises the leader/validator
execution split sketched in :doc:`/spec/01-core-architecture/02-vm`. The actual
consensus algorithm (block production, voting, finality) is owned by the node, not
by GenVM; what follows is the GenVM-side machinery that produces the artifacts the
node consumes.

Roles and the ``is_leader`` flag
--------------------------------

A GenVM run is either a *leader* run or a *validator* run, decided once when the
node starts the run. The supervisor exposes this as
``Supervisor::is_leader()`` (``executor/src/rt/supervisor/mod.rs:209``); the flag is
derived from whether the node has handed in opaque leader public data:

- Leader run: ``leader_public_data == None``. Every ``RunNondet`` call executes the
  leader sub-program and the result is pushed into the supervisor's
  ``nondet_results`` vector, indexed by ``call_no``.
- Validator run: ``leader_public_data == Some(bytes)``. The executor decodes the
  bytes into the leader's results. ``RunNondet`` retrieves the result at ``call_no``
  and feeds it back to the contract as the second argument of the non-det block. If
  the leader produced fewer entries than the validator demands, the run aborts with
  ``VmError::leader_fault().nondet_output().absent()``.

``call_no`` is a monotonically increasing counter incremented per ``RunNondet``
invocation. The hard cap is ``internal_constants::top_limits::NONDET_BLOCKS`` (``4096``);
exceeding it produces ``VmError::oom().ram().limit()``. The counter is what binds
a leader's i-th non-det result to the validator's i-th non-det check — the
ordering of ``RunNondet`` calls in deterministic code MUST match between leader
and validator, otherwise the contract is non-replayable.

The leader data encoding is defined by :ref:`gvm-def-leader-output-format`.

Validator Comparison
--------------------

GenVM itself does not run the agreement algorithm. After the validator finishes
executing the non-det block over the leader's result, it returns its own
``ResultCode``/payload pair to the host. The host (or a Lua greybox script — see
:doc:`/impl-spec/03-greyboxing/index`) compares the two and decides whether to
emit an "agree" or "disagree" vote.

For prompt-template calls (``EqComparative`` /
``EqNonComparativeLeader`` / ``EqNonComparativeValidator``) the comparison is
performed *inside* the LLM module's Lua entry point: the validator script receives
the leader's answer and the validator's own answer (or context) and returns a
boolean. The boolean is the agreement vote. See
:doc:`/impl-spec/03-greyboxing/index` "Template Contracts".

For raw ``RunNondet`` blocks, agreement is whatever the contract code chooses to
return from its validator function — typically a boolean.

Disagreement and Timeouts
-------------------------

A validator that produces a different result than the leader simply returns it; no
GenVM-internal mechanism flags the divergence. The node decides what to do with the
disparate votes.

Timeouts are enforced two layers above GenVM:

- The manager kills a GenVM child process when its overall session exceeds the
  configured budget.
- The fee evaluator (see :doc:`/impl-spec/04-fees`) drains the relevant bucket and
  causes the next ``consume_*`` call to fail with an OOM-class error.

Anti-Cheating
-------------

The error fingerprinting mechanism described in
:doc:`/spec/01-core-architecture/02-vm` "Error Fingerprinting" is what prevents a
validator from voting "agree" without doing the work: any error result is bundled
with a BLAKE3 hash of the WASM memory at the failure point, and a node fabricating
errors without execution cannot produce a matching hash. There is no analogous
fingerprint for the *successful* path — agreement on successful non-det results
relies on the validator actually re-running the contract under the leader's
substituted values, which is what the GenVM run-loop forces it to do.
