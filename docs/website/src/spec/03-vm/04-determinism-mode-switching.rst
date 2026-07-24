Switching To :ref:`gvm-def-non-det-mode`
========================================

When requesting a non-deterministic execution, a new :term:`sub-VM` is created.
Which of the three modes below applies is fixed for the whole execution: a node
runs as leader when it computes the non-deterministic result itself, and as
validator or in sync mode when it is handed one.

.. _gvm-def-leader-mode:

Leader Mode
-----------

Returns the :ref:`gvm-def-vm-result` produced by the :term:`sub-VM`, with one
change before it is published and before it enters the execution hash: a
:ref:`gvm-def-vm-error`'s ``" # <detail>"`` suffix is **stripped**. The
non-deterministic result channel carries bare codes only, so what the leader
publishes is exactly what an honest validator accepts under
:ref:`gvm-def-proposed-result-validity`.

The leader does **not** apply that acceptance check to its own result. Doing so
could only rewrite an honest result into the
:ref:`gvm-def-derived-outcome-namespace`, which every validator would replace
again — an execution-hash mismatch between two honest nodes.

.. _gvm-def-sync-mode:

Sync Mode
---------

The proposed result is accepted or replaced per
:ref:`gvm-def-proposed-result-validity` and returned. There is no vote to cast,
so the resulting :ref:`gvm-def-vm-result` is simply the call's result.

.. _gvm-def-validator-mode:

Validator Mode
--------------

The proposed result is accepted or replaced per
:ref:`gvm-def-proposed-result-validity`, then handed to the :term:`sub-VM` for
comparison. That :term:`sub-VM` must :ref:`gvm-def-return` a ``bool`` value:
whether the validator accepts the leader's result. Any other result has the same
effect as producing ``bool(false)``.

A replaced proposal takes this same path — the derived code is compared like any
other, so the vote stays contract-controlled and no branch bypasses the
comparison. The replacement is a pure function of the proposed bytes, so every
honest validator derives the same value and votes identically.

Returns the accepted or replaced result.

Fees
----

The accepted or replaced result is charged on **its own** length, not on the
length of the proposed bytes: rejected bytes never enter the result or the
execution hash.
