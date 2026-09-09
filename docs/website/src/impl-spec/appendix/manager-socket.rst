Manager Socket Protocol
=======================

The manager exposes a long-lived WebSocket for driving executions. It replaces
the HTTP run flow (``POST /genvm/run`` + poll + ``DELETE``), which remains
available for one release train as a deprecated adapter over the same core
(see :doc:`manager-api`). The admin HTTP endpoints are unaffected.

Clients connect by upgrading ``GET /ws`` on the manager's HTTP listener, so the
protocol is reachable wherever that listener is: over the unix socket when the
manager runs with ``--socket``, over TCP when it runs with ``--host``/``--port``.
There is no separate address to configure.

No authentication: the manager assumes a trusted deployment (unix socket or
loopback); do not expose this listener beyond the machine boundary.

.. toctree::
   :hidden:
   :maxdepth: 1

   manager-socket-consts

Framing
-------

Every protocol message is one **binary** WebSocket message; WebSocket already
delimits messages, so there is no length prefix. Header integers are
big-endian. Payloads are calldata-encoded (see
:doc:`/spec/04-contract-interface/01-calldata`).

::

   message    := method_id:u16_be request_id:u64_be payload
   payload    := calldata(value)

A message is therefore ``payload length + 10`` bytes. Note the byte order
differs from the executor host protocol, which is little-endian
(:doc:`host-loop`).

- ``request_id != 0``: a request. Exactly one reply message follows, with the
  same ``method_id`` and ``request_id`` echoed (or an ``error`` message with the
  ``request_id`` echoed).
- ``request_id == 0``: a notification; no reply. Only the manager sends
  notifications. A client message with ``request_id == 0`` is answered with an
  ``error`` (``bad_request_id``, ``request_id == 0``).

Request ids are chosen by the client and scoped to the connection; the manager
never initiates requests, so there is no id-space split.

Unknown method ids, undecodable payloads, messages shorter than the 10-byte
header, and text messages each produce an ``error`` message without closing the
connection.

A message larger than the manager's ``max_message_bytes`` is refused by the
WebSocket layer before the manager sees it. The cap is enforced while reading,
and reports as a read failure rather than a close handshake, so the connection
is **dropped without a close frame** -- a client observes an abnormal closure
(1006), not 1009, and no ``error`` payload is sent. Clients that page artifacts
within the documented chunk cap never approach the cap.

Payloads are externally-tagged: a single-key map whose key names the variant.
The ``method_id`` header is authoritative for routing; the in-payload tag is
redundant by design (log readability).

Method ids
----------

Generated from ``crates/modules-interfaces/codegen/data/manager-api.json``
(Rust, Python and this page share one source).

The generated constants are rendered in :doc:`manager-socket-consts`; see
:ref:`gvm-def-enum-methods` for the method id enum. Directions and request
kinds are described in each method section below.

Connection hello
----------------

Immediately after accepting a connection the manager sends a ``hello``
notification::

   { "hello": { "boot_id": u64, "protocol_major": u32 } }

``boot_id`` is random, generated once per manager process start. GenVM ids
restart from 1 with the process, so the durable identity of a run is the pair
``(boot_id, genvm_id)``. Clients MUST remember the ``boot_id`` and pass it to
``attach``; a mismatch after a manager restart is surfaced as
``boot_id_mismatch`` instead of silently binding to an unrelated, reused id.

``run``
-------

Request payload: the same logical structure as the deprecated
``POST /genvm/run`` body (``GenvmRunRequest`` in :doc:`manager-api`), plus:

- ``host_genvm_id`` (string, optional) -- client correlation token, echoed in
  events for this run. Also an idempotency key: a ``run`` repeated with the
  same token before the retention TTL expires returns the id already
  allocated for it instead of starting a second execution.
- ``host_hello_data`` (array of bytes, optional, default ``[]``) -- indexed
  by host connection index; the executor writes entry *i* verbatim to host
  *i* on connect, before the first method byte (see :doc:`host-loop`). The
  manager rejects a non-empty entry for a host connection it owns itself
  (currently index 1, the ``consume_result`` socketpair).
- ``hook_cross_contract_calls`` (bool, optional, default ``false``) -- whether
  this host wants to be asked where a ``CallContract`` runs. When false the
  manager answers ``resolve_call_contract_executor`` itself with a null reply,
  so every call stays in-process and the host need not implement that method.
  When true the question is routed to host 0 and the host may send the caller
  across a major boundary (see :doc:`host-loop`). A nested run inherits the
  value from its parent.
- ``deadline`` (duration string, optional) -- when set, overrides
  ``max_execution_minutes`` as the strict deadline. The manager enforces it
  and pushes the terminal event; clients need no timeout timer of their own.
  Either way the deadline is capped at 24 hours: a longer one is silently
  shortened, not rejected. See `Duration strings`_.
- ``unsafe_overrides`` (map, optional) -- overrides that reach boundaries
  production traffic cannot. Each member states the ``debug_mode`` it needs;
  with debugging disabled none of them apply, so consensus traffic always runs
  the manifest-resolved version with the executor's own limits.

  - ``reroute_to`` (string, optional) -- run this version instead of the one
    ``selector`` resolves to. A plain string is an executor directory used as it
    stands; a ``re:`` prefix makes the rest a regular expression matched against
    manifest version keys, and the newest match wins. Honored from ``safe``.
  - ``initial_recursion`` (u32, optional) -- seeds the chain's recursion
    budget, replacing the executor's own ``VM_RECURSION``, so a boundary test
    need not spend one executor process per unit of budget. Honored from
    ``unsafe``.

Response::

   { "genvm_id": u64 }

The id is allocated and returned immediately; validation, permit acquisition
and process spawn continue asynchronously and report through ``event``
notifications. The requesting connection is subscribed to the run's events
automatically.

``attach``
----------

::

   { "attach": { "boot_id": u64, "genvm_id": u64 } }

Subscribes the connection to the run's events and returns a snapshot -- the
most recent lifecycle event for the run, in the same shape as an ``event``
payload::

   { "snapshot": <event payload> }

Errors: ``boot_id_mismatch`` if ``boot_id`` is not the current process's;
``unknown_id`` if the run does not exist, was acked, or its retention TTL
expired. Disconnecting drops all of a connection's subscriptions; it does not
affect the run (see below).

``cancel``
----------

::

   { "cancel": { "genvm_id": u64 } }

Requests termination. Response is an empty map; the outcome arrives as the
run's single terminal event. Cancelling a run still queued on permits aborts
it before spawn (no permit is consumed) and still yields exactly one terminal
event with cause ``cancelled``.

``ack``
-------

::

   { "ack": { "genvm_id": u64 } }

Releases the retained result and state for a finished run. Response is an
empty map. An ``ack`` before a terminal event or result is refused with
``not_finished`` and leaves the run fully usable. After ``ack`` (or after the
retention TTL expires), ``attach`` and ``get_artifact`` answer ``unknown_id``.
Reads before ``ack`` are non-destructive and repeatable from any number of
connections.

``get_artifact``
----------------

::

   { "get_artifact": { "genvm_id": u64, "field": str,
                       "offset": u64, "max_len": u32 } }

``field`` is one of ``stdout``, ``stderr``, ``genvm_log``. Response::

   { "total_len": u64, "data": bytes }

``data`` is at most ``min(max_len, chunk_cap)`` bytes starting at ``offset``
(``chunk_cap`` is a server constant, 256 KiB); clients page until
``offset + len(data) == total_len``. ``genvm_log`` is served as JSON Lines
(one structured record per line). Artifact replies are sent on the same
connection through a low-priority writer queue, so bulk transfers cannot
starve lifecycle events.

The terminal event carries each artifact's total size, so clients can skip
the calls entirely when the blobs are empty.

``event`` notifications
-----------------------

Externally-tagged; every event carries ``genvm_id`` and, when the run was
started with one, ``host_genvm_id``. Variants:

``queued``
   The run is allocated but no executor process exists yet, because it is
   waiting for a permit. Non-terminal. Mainly seen as the ``attach`` snapshot of
   a run that has not spawned; a client that attaches this early always observes
   the current lifecycle state and every terminal event, but intermediate states
   may be coalesced, so a fast run can go straight from ``queued`` to a terminal
   event. ::

      { "queued": { "genvm_id": u64, "host_genvm_id": str? } }

``started``
   The executor process was spawned. ::

      { "started": { "genvm_id": u64, "host_genvm_id": str? } }

``failed_to_start``
   Terminal. The run never reached a spawned executor: request validation,
   version resolution, or the spawn itself failed. Permits are released. ::

      { "failed_to_start": { "genvm_id": u64, "host_genvm_id": str?,
                             "error": str } }

   An executor that *was* spawned and then died on its own -- rejecting its
   arguments, say -- reports ``finished`` with that exit code, not this event.
   The two are distinguished by whether a process was created, which the
   manager knows exactly; "started, then died immediately" is not a state it
   can observe without guessing, and a timer that guesses it would both tax
   every healthy run and still race a slow failure.

   Either way the host receives a terminal event promptly, which is what
   matters: this is the class of failure that used to leave it waiting on
   ``accept()`` forever.

``finished``
   Terminal, sent exactly once per run. ::

      { "finished": {
          "genvm_id": u64, "host_genvm_id": str?,
          "cause": str,            # exited | cancelled | deadline | shutdown
          "exit_code": i64?,       # null when killed before exit code known
          "consumed_result": bytes?,  # what the executor sent via consume_result
          "metrics": map?,
          "finished_at": str,      # RFC3339
          "version_major": u32, "version_minor": u32,
          "artifact_sizes": { "stdout": u64, "stderr": u64,
                              "genvm_log": u64 } } }

For a top-level run, ``consumed_result`` is one outer ``ResultCode`` byte
followed by a calldata-encoded ``ReportedResult`` map. Before retaining it, the
manager checks that:

#. The outer byte is a known result code and agrees with the map's ``kind``
#. The map decodes completely
#. ``execution_hash`` and ``small_hash`` are each 32 bytes unless the result is
   ``InternalError``

An invalid report is refused without an acknowledgement and is not published
as ``consumed_result``. ``FatalVmError`` is also illegal at this boundary: a
debug manager asserts, while a release manager logs the executor violation and
rewrites both result-code locations to ``VmError`` before publication. Clients
therefore never receive a top-level ``FatalVmError``

The manager does not decode reported ``leader_public_data``. The bytes remain
opaque, executor-line-specific consensus proposals

Lifecycle guarantees:

- Exactly one terminal event (``failed_to_start`` or ``finished``) per run,
  delivered to every connection subscribed at the time.
- **Disconnect does not kill a run.** Runs terminate only via ``cancel``, the
  deadline, or manager shutdown. A client may disconnect, reconnect, and
  ``attach`` by ``(boot_id, genvm_id)`` to recover the state and result.
- Results are retained until ``ack`` or the retention TTL (manager
  configuration ``execution_retention``, a duration string, default ``5m``).

Duration strings
----------------

Every duration on this wire and in the manager configuration is a string:
a decimal number followed immediately by a unit, with no space.

============  ======================
Unit suffix   Meaning
============  ======================
``ms``        milliseconds
``s``         seconds
``m``         minutes
``h``         hours
============  ======================

Examples: ``"30s"``, ``"10.5m"``, ``"1h"``, ``"250ms"``. The fractional part
is optional and is resolved at millisecond granularity; a value that is not a
number followed by one of the units above is rejected as ``malformed_frame``
(or as a configuration error at startup).

A bare number is **not** accepted -- the unit is mandatory, so that a value
can never be silently misread as the wrong scale.

Error messages
--------------

``method_id = error``, ``request_id`` echoed from the failing request
(``0`` when no request id could be attributed)::

   { "code": u8, "message": str }

See :ref:`gvm-def-enum-errors` for the generated error code enum. Meanings:
``internal`` is a handler failure with a diagnostic message;
``malformed_frame`` is failed calldata decode, a bad payload shape, a message
shorter than the header, or a non-binary message; ``unknown_method`` is a
method id not in the generated table; ``unknown_id`` means the run never
existed, was acked, or expired; ``boot_id_mismatch`` is ``attach`` across a
manager restart; ``bad_request_id`` is a client request with
``request_id == 0``; and ``not_finished`` is ``ack`` before a terminal event
or result.

An oversized message has no error code: the connection is dropped, as described
under `Framing`_.

Scope note
----------

Multiple connections attaching to one run observe it through the *manager*
channel only. Host connection 0 -- the socket the executor dials and speaks
the host protocol on (:doc:`host-loop`) -- is owned by whichever address the
``run`` request supplied; attaching does not share it. ``host_hello_data`` is
the building block for future shared-host schemes.
