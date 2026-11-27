Log Record Format
=================

Every log line the executor and the manager emit is one JSON object. The same
shape is used on stderr, in the manager's own log and in the ``genvm_log``
artifact served over the manager socket (see :doc:`manager-socket`).

Fields
------

.. list-table::
   :header-rows: 1
   :widths: 16 14 70

   * - Key
     - Type
     - Meaning
   * - ``level``
     - string
     - One of ``trace``, ``debug``, ``info``, ``warn``, ``error``. Always the first key
   * - ``audience``
     - string
     - Who the record is meant for, see below. Always the second key
   * - ``target``
     - string
     - Rust module path of the call site
   * - ``message``
     - string
     - Human-readable text
   * - ``ts``
     - string
     - Wall-clock timestamp
   * - *anything else*
     - any
     - Structured captures of the call site, see `Limits`_

Limits
------

1. Outside ``trace`` level, a byte buffer keeps at most 128 bytes and a string
   at most 1024: the first and last halves, joined by ``...``. An interpolated
   ``message`` counts as a string; a literal one is kept whole. An integer
   wider than 256 bits is shown by its width
2. A record dumps at most 256 values in all, nested ones, map keys and error
   sources included. A value past that is written as ``"$TooMany"``, and a map
   past it ends with ``"$TooMany": true``. Elements a long sequence drops spend
   the budget too
3. Nesting, error source chains included, stops at depth 64 with ``"$TooDeep"``;
   a sequence of more than 128 elements keeps its first and last 64 around a
   ``"$TooMany"``

``audience`` is a reserved key: a capture named that way fails to compile and a
hand-built record carrying one has it dropped.

Audience
--------

The audience is a tag only, it never takes part in filtering. It answers *who
should act on this line*:

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - Value
     - Reader
   * - ``user``
     - The contract developer or caller: the record is a consequence of the contract's
       own input, code or budget
   * - ``operator``
     - The node runner: configuration, environment, providers, broken internal
       invariants, panics
   * - ``introspector``
     - Someone debugging GenVM itself: lifecycle tracing, state dumps, timings. The
       default when a call site names none

Rules that follow from the tag:

1. A ``user`` record may only carry scalar captures (display, error, debug,
   id); bulk dumps such as calldata, byte buffers and serialised values are
   rejected at compile time, and both the byte and the string limit are clamped
   to 128, ``trace`` level included
2. Panics are ``operator``
3. Records from a v0.2.x executor, which knows nothing about audiences, are
   tagged ``introspector`` by the manager
4. A line the executor emitted that is not valid JSON is wrapped by the manager
   into ``{"level": "error", "audience": "operator", "message": "genvm log",
   "line": <base64>}``

Lua scripts log through ``lib.log { level = ..., audience = ..., message = ... }``;
an absent or unknown ``audience`` means ``introspector``.

Manager Sink
------------

Under ``bounded`` capture (see :doc:`../01-core-architecture/04-executor`) the
manager keeps at most 128 records per execution and evicts by audience rather
than by age:

1. While the cap has never been hit, everything is queued
2. On the first overflow every ``introspector`` record is dropped, a marker
   ``{"level": "warn", "audience": "operator", "message": "introspector logs dropped"}``
   is appended, and from then on ``introspector`` records are discarded on arrival
3. On the next overflow the sink degrades to a plain queue that drops the oldest
   ``user`` or ``operator`` record; ``introspector`` records stay discarded

The marker counts toward the cap. ``unbounded`` capture keeps every record.
A line that is not a JSON record keeps at most 4 KiB, cut as strings are. A
log line over 1 MiB is never parsed: only its first 1 MiB is read, and it is
kept as such a text line.
The manager reads each executor's log pipe to EOF before collecting the
execution result, including on early process exit
