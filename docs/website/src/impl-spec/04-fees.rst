Fees, Buckets and Expressions
=============================

GenVM charges for run-time resources through a small, operator-defined expression
language. The configuration is consumed by ``executor/src/rt/fees.rs`` and the
expression evaluator lives in ``executor/crates/common/src/expr/``.

Three independent moving parts are involved:

#. **Buckets** — named ``U256`` reservoirs that GenVM debits during execution.
   Their initial totals come from the host (typically the consensus layer) on a
   per-call basis.
#. **Bucket configs** — operator-provided rules that bind a *named* charge (storage
   pages, message receipts, nondet output bytes, message fees) to a *bucket name*
   and an expression that computes the per-event cost.
#. **Expressions** — a small typed lambda calculus used to write the cost rules. It
   is evaluated once at startup (for ``subtract_on_start_expr``) and once per
   charge (for ``delta_expr``).

Configuration
-------------

The ``fees`` block of the GenVM config (see ``doc/schemas/default-config.json``,
``genvm-fees-conf``) has the following shape::

   "fees": {
     "expr_prelude": "<expression>",
     "storage":         { "buckets": "execution_data_gas", "subtract_on_start_expr": "...", "delta_expr": "..." },
     "message_receipt": { "buckets": ["execution_data_gas", "submitted_messages", "submitted_messages_count"], "subtract_on_start_expr": "...", "delta_expr": "..." },
     "nondet_output":   { "buckets": ["execution_data_gas", "nondet_outputs"], "subtract_on_start_expr": "...", "delta_expr": "..." },
     "message_fee":     { "buckets": "message_fee", "subtract_on_start_expr": "...", "delta_expr": "..." },
     "event":           { "buckets": "execution_data_gas", "subtract_on_start_expr": "...", "delta_expr": "..." }
   }

``expr_prelude``
   Shared expression text prepended to every bucket expression before parsing. The
   intended use is shared ``let`` bindings (or Y-combinator definitions) that the
   per-bucket expressions can reference.

``buckets``
   Either a single name, or an array of names, in the bucket-total map the host
   passes to ``DataLimit::new``. With an array, a scalar ``delta_expr``
   result is charged against every listed bucket, while an array result is charged
   element-wise (lengths must match); all debits in one charge are atomic
   (all-or-nothing). Two bucket configs MAY share the same name; in that case both
   charge against the same reservoir. The ``message_fee`` / ``message_receipt`` pair
   has special atomic-debit behaviour when they share a bucket

``subtract_on_start_expr``
   A numeric expression or array of numeric expressions. It follows the same scalar
   versus element-wise array rule as ``delta_expr``, including exact array-length
   matching. Evaluated once at startup with ``node`` bound to the host-provided gas
   constants (see below). The resulting ``U256`` values are debited immediately.
   Defaults to ``"0"`` if omitted.

``delta_expr``
   A function ``\attrs = body`` (one or more ``\`` lambdas) whose ``attrs``
   parameter is an object containing the variables that the charge depends on.
   Evaluated once per charge; the resulting integer is debited from the bucket.

The five fee rules and their ``attrs`` shape are:

==================== ======================================================================
Name                 ``attrs`` keys
==================== ======================================================================
``storage``          ``pages``: number of pages allocated
``message_receipt``  ``isFirstMessage``, ``isInternal``, ``isDeploy``,
                     ``rotationsCount``, ``calldataLength``, ``codeLength``,
                     ``subtreeLength``
``nondet_output``    ``outputLength``
``message_fee``      ``isInternal``, ``matchedFeeParams``: an object with
                     ``leaderTimeunitsAllocation``,
                     ``validatorTimeunitsAllocation``, ``executionBudgetPerRound``,
                     ``rotations`` (array)
``event``            ``blobSize``, ``topicsCount``
==================== ======================================================================

The ground truth for these names is ``fees.rs`` (search for ``calculate_bucket``
call sites). ``isFirstMessage`` is true until the first message emission is
successfully accumulated; event emissions do not clear it.

The Expression Language
-----------------------

Source files: ``executor/crates/common/src/expr/``.

The language is a pure, call-by-need lambda calculus over rationals, booleans,
strings, arrays and (sorted) objects. It is **deliberately small** and runs without
fuel or recursion limits — the comment in ``evaluator.rs:8`` reads:

   ``SAFETY: this evaluator has no recursion depth or fuel limits. It is only used
   for trusted, operator-supplied fee config expressions, never for
   contract-supplied or user-supplied input.``

Operators MUST NOT expose this surface to contract or end-user input.

Syntax
~~~~~~

::

   expr   ::= let NAME = expr in expr
            | if expr then expr else expr
            | \NAME [NAME...] = expr            -- lambda; multi-param is sugar
            | comparison
   comparison ::= sum (('<' | '>' | '<=' | '>=' | '==' | '!=') sum)?
   sum    ::= product (('+' | '-') product)*
   product ::= unary (('*' | '/') unary)*
   unary  ::= '-' unary | application
   application ::= primary (primary)*
   primary ::= NUMBER | NAME | STRING | array | object | '(' expr ')' | primary '.' NAME
   array  ::= '[' (expr (',' expr)*)? ']'
   object ::= '{' (NAME '=' expr ';')* '}'
   STRING ::= '"' ( char | '\(' expr ')' )* '"'   -- supports interpolation

``let`` is **non-recursive**: the body of ``let f = ... in ...`` cannot reference
``f``. Recursion is expressed with a Y combinator, e.g.::

   let Y = \f = (\x = f (x x)) (\x = f (x x)) in
   let fact = Y (\rec n = if n <= 1 then 1 else n * rec (n - 1)) in
   fact 5

Strings support ``\(expr)`` interpolation; ``toString`` and ``\(..)`` use the same
rendering.

Types
~~~~~

- ``number`` — arbitrary-precision rational (``BigRational``). Integer division of
  two rationals yields a rational; use ``floor`` before converting back to ``U256``
  if you need truncation.
- ``bool``
- ``string`` (``Arc<str>``)
- ``array`` (``Arc<Vec<Value>>``)
- ``object`` (sorted ``BTreeMap<String, Value>``, accessed via ``.`` or ``hasKey``)
- ``function`` (host or guest, both call-by-need)

Type errors are reported eagerly; division by zero is a runtime error.

Builtins
~~~~~~~~

The following names are predefined by the evaluator (``resolve_builtin`` in
``evaluator.rs``):

- ``floor n`` — floor of a rational.
- ``toString v`` — render any value as a string.
- ``hasKey obj k`` — whether the object has the given key.
- ``arrayLen a``, ``arrayGetElem a i`` — array length and indexing.
- ``pow base exp`` — integer-exponent power (negative exponents take the reciprocal;
  ``pow 0 (-n)`` is a division-by-zero error).

Free Variables and ``node``
~~~~~~~~~~~~~~~~~~~~~~~~~~~

A free variable not satisfied by the surrounding ``let`` bindings or the builtin
table is looked up via the host-provided ``get_var``. In the fee evaluator the only
free variable allowed at the top level is ``node``: an object built from the
``gas_data`` map handed to ``DataLimit::new``. Each ``(name, raw)`` pair in
``gas_data`` is itself evaluated as an expression (against ``expr_prelude``) and the
result is inserted into ``node`` under ``name``.

Bucket expressions reference these as e.g. ``node.gasPerChangedSlot`` and the
node-provided values let consensus parameters drift independently of the executor
binary.

``node.overlaySplitBps`` carries the combined developer and DAO share of the
time-unit fee pool. Internal-message primary reserves use::

   timeUnitPool
   + floor(timeUnitPool * overlaySplitBps / (10000 - overlaySplitBps))
   + executionTerm

This minimum primary fee is the same for messages emitted on acceptance and
finalization. The execution term is not grossed up. Omitting the field makes
internal-message fee evaluation fail rather than silently undercharge

The closing of ``node`` happens at startup:
``\node = <prelude> <code>`` is parsed and applied to the resolved ``node`` object.
The returned value (a number for ``subtract_on_start_expr``, a function for
``delta_expr``) closes over ``node`` and has **no remaining free variables**; any
later lookup at charge time is a configuration bug and is reported as
``UndefinedVariable``.

Evaluation Model
~~~~~~~~~~~~~~~~

- **Lazy.** Argument thunks are forced on first use and memoised
  (``Thunk::force`` in ``value.rs``). A self-referential force returns
  ``"infinite recursion while forcing a lazy value"`` rather than deadlocking.
- **Pure.** No I/O, no mutation, no clock access. The only nondeterminism the
  evaluator can produce is the result of host functions, which in the fee surface
  is a no-op (``no_free_vars`` is the resolver).

Charge Lifecycle
----------------

Per process, per non-batched call:

#. The host invokes ``DataLimit::new(bucket_totals, fees_cfg, gas_data)``.
#. The evaluator parses ``expr_prelude + gas_data[i].value`` for every ``gas_data``
   entry and builds the ``node`` object. Parse or evaluation errors here surface as
   ``parsing gas_data constant 'X'`` / ``evaluating gas_data constant 'X'``.
#. For each of the five fee rules the evaluator builds ``\node = <prelude> <code>``
   for ``subtract_on_start_expr`` and ``delta_expr``, applies the ``node`` object,
   and stores the resulting ``Value`` (a rational and a function respectively).
#. For top-level runs, the startup cost is debited immediately. Underflow surfaces
   as the bucket's configured OOM ``VmError`` (see the ``VmError::oom().*`` enum
   chain at the call site). Nested runs receive zero-valued bucket placeholders and
   skip startup debits because their caller already paid them.
#. During the run, every ``consume_*`` function in ``fees.rs`` packs the per-event
   ``attrs`` into an object, applies the bucket's ``delta`` function, converts the
   result to ``U256`` (rejecting non-integers and negatives), and debits the
   reservoir.

Bucket totals are mutated under a single ``tokio::sync::Mutex<HashMap<String,
U256>>`` so the debits are sequentially consistent across concurrent sub-VMs.
``message_fee`` and
``message_receipt`` debits are atomic with respect to each other: when they share a
bucket name, the costs are summed and debited once; otherwise both reservoirs
are checked before either is decremented. This avoids partial charges when a
combined send-and-deliver flow runs out of fee budget mid-call.

Failure Modes
-------------

- **Parse error** — bad prelude/expr syntax. Surfaced as
  ``parsing <label> fee expression``; the GenVM process refuses to start.
- **Evaluation error** at startup — type mismatch, undefined variable, division by
  zero. Same surface; refuses to start.
- **Type / range error** during a charge — the delta returned a non-integer, a
  negative, or a value above ``U256::MAX``. Logged as
  ``failed to evaluate fee expression`` and propagated to the caller; the cost is
  treated as undefined and the charge fails closed.
- **Bucket underflow** at startup — ``subtract_on_start`` exceeds the bucket total.
  Returns the bucket's OOM ``VmError`` immediately.
- **Bucket underflow** during a charge — ``consume_bucket_raw`` returns ``false``.
  The caller turns this into an OOM-class error
  (``oom().storage()`` for ``consume_storage_pages``,
  ``oom().receipt().nondet_output()`` for ``consume_nondet_output``, etc.).

Configuration Sketch
--------------------

A minimal, illustrative ``fees`` block::

   "fees": {
     "expr_prelude":
       "let Y = \\f = (\\x = f (x x)) (\\x = f (x x)) in",
     "storage": {
       "buckets": "execution_data_gas",
       "subtract_on_start_expr": "node.gasPerStorageBootstrap",
       "delta_expr":              "\\a = a.pages * node.gasPerStoragePage"
     },
     "message_receipt": {
       "buckets": "message_fee",
       "delta_expr":
         "\\a = if a.isDeploy then node.gasPerDeployByte * (a.calldataLength + a.codeLength)\n         else node.gasPerCallByte * a.calldataLength"
     },
     "nondet_output": {
       "buckets": "nondet_outputs",
       "delta_expr": "\\a = a.outputLength * node.gasPerNondetByte"
     },
     "message_fee": {
       "buckets": "message_fee",
       "delta_expr":
         "\\a = if a.isInternal\n        then node.gasInternal * arrayLen a.matchedFeeParams.rotations\n        else a.matchedFeeParams.gasLimit * a.matchedFeeParams.maxGasPrice"
     },
     "event": {
       "buckets": "execution_data_gas",
       "delta_expr": "\\a = (a.topicsCount + a.blobSize) * node.gasPerStoragePage"
     }
   }

``message_fee`` and ``message_receipt`` share the ``message_fee`` bucket here, so an
outbound message debits both costs atomically against it.

Message-Fee Allocation Matching
-------------------------------

The ``a.matchedFeeParams`` above is selected per outbound message from the call's
allocation list (``accumulator.message_fee_allocation``). Kind is matched first
(``External`` for ``EmitExternalMessage``, else ``Internal``), followed by exact
``recipient`` and ``call_key``. A per-recipient ``call_key`` wildcard is tried only
after the exact key; executor-only open-bucket recipient wildcards are less specific
than either. List order therefore cannot make a wildcard shadow an exact allocation.

For internal messages, ``on`` is checked after the allocation key is resolved, so a
phase mismatch on an exact allocation does not fall through to a wildcard. For
external messages, an exhausted exact allocation spills to the per-recipient
``call_key`` wildcard. If neither key has an allocation, the external message uses
the legacy unallocated path and consumes only its receipt cost. Existing but exhausted
candidates yield an allocation-budget error.

Funding modes
~~~~~~~~~~~~~~

An outgoing internal message is funded one of two ways:

- **Allocation-matched (default).** The fee is matched against the allocation
  tree as above. After the expression computes the primary reserve, the executor
  adds the budgets of the matched node's direct children; deeper budgets are
  already contained by their direct parent. A declared budget exceeding the
  matched node's remaining ``budget`` is rejected with an allocation-budget
  error; otherwise it consumes ``message_fee`` atomically with the
  ``message_receipt`` charge.
- **Balance-funded (``use_balance``).** When an ``EmitInternalMessage`` /
  ``EmitInternalDeployMessage``
  sets ``use_balance`` (the chain's ``useBalance``, gated on
  :ref:`gvm-perm-use-balance-for-message-fees`), allocation matching is skipped
  entirely. The fee is metered from the guest-supplied ``fee_params`` and that
  metered amount is the child's ``declaredBudget``, reserved from the emitting
  contract's balance (jointly with ``value``; insufficient balance yields
  ``InsufficientBalance``). The ``message_fee`` bucket is **not** consumed (the message is
  excluded from the sender pool on-chain); only ``message_receipt`` is. The emitted
  allocation subtree is empty, so nested child messages must each fund themselves.

For either phase, an internal message therefore declares::

   minPrimaryFees(feeParams) + sum(directChildAllocation.budget)

For balance funding the sum is zero. The primary fee already covers the child's
configured lifecycle, including appeals; the remainder becomes the child's
message-fee bucket. External messages declare zero

The parent allocation's aggregate capacity is a separate invariant. If
``L = appealRounds + 1`` novel executions may each emit a child carrying budget
``C``, preserving that descendant capacity for every execution requires::

   allocationBudget >= L * (minPrimaryFees + C)

The lifecycle multiplier therefore belongs to this parent allocation-capacity
check, not to one emitted child's declared budget

The current minimum ``L * minPrimaryFees + C`` reserves ``C`` only once. Whether
an allocation promises descendant capacity once or once per accepted execution
remains unspecified; this does not change the per-message formula

  Because ``fee_params`` is guest-supplied, it is validated before it reaches the
  fee evaluator (``validate_balance_fee``): empty ``rotations`` and any zero price
  cap (``max_price_gen_per_time_unit`` / ``storage_fee_max_gas_price`` /
  ``receipt_fee_max_gas_price``) are rejected with ``Inval``, matching the chain's
  reveal-time ``FeeValueMustBeNonZero`` checks. Magnitudes are bounded too
  (prices/budgets below 2\ :sup:`96`, counts — time units and rotations
  entries — below 2\ :sup:`32`) so the worst-case metered floor provably fits
  in ``U256`` without saturating.

  The floor replicates the chain's ``minMessagePrimaryFees``: the consensus term
  is charged at the guest's ``max_price_gen_per_time_unit`` funding cap, then the
  developer and DAO overlay is grossed up on that time-unit pool. Two further
  node-configured floors fire as
  ``VMError``\ s: ``fee below_minimum`` when a non-zero ``execution_budget_per_round``
  is below ``node.messageBudgetFloor`` (the chain's ``BudgetTooLow``), and
  ``fee too_many_rounds`` when ``rotations`` implies more rounds than the validator
  table (``node.validatorsPerRound``) covers. Unless both allocations are zero,
  the leader and validator time units must also fit the host-provided
  ``minProposeTimeout`` / ``maxProposeTimeout`` and ``minCommitTimeout`` /
  ``maxCommitTimeout`` ranges; violations produce
  ``fee phase_timeout_out_of_bounds`` on both funding paths.
