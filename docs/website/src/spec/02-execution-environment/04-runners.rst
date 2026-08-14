.. _runners-reference:

:term:`Runners <Runner>`
========================

:term:`Runners <Runner>` specify execution environments for GenVM contracts.

:term:`Runner` Architecture
---------------------------

Identification and Packaging
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A :term:`runner` is referenced by a runner id, which has one of the following
forms (see the ``runner-id`` definition in the runner.json schema):

- ``<human-readable-id>:<hash>`` — a packaged runner. ``human-readable-id`` is
  provided for convenience; ``hash`` is a hash of its contents (see `Hash Format`_).
- ``contract`` — the runner of the contract currently being executed.
- ``chain:<address>[:<a|f>[:<slot>]]`` — a runner code blob read from a storage
  slot of an arbitrary contract (``a`` = accepted, ``f`` = finalized). ``<address>``
  is a ``0x`` 20 byte hex address and ``<slot>`` is a :term:`SlotID` encoded with
  :doc:`../04-contract-interface/06-gvm32`. Both ``<a|f>`` and ``<slot>`` are
  optional: ``<a|f>`` defaults to ``a`` and ``<slot>`` to the target contract's
  root code slot.
- ``custom:<hash>`` — a runner registered at runtime via the ``RegisterRunner``
  ``gl_call``, looked up by its hash.

``contract``, ``chain`` and ``custom`` are reserved prefixes and cannot be used
as human-readable ids.

.. _gvm-def-chain-runner-state:

``chain:`` State Visibility
~~~~~~~~~~~~~~~~~~~~~~~~~~~

A ``chain:`` id is code, but its resolution is an ordinary
:ref:`gvm-def-det-mode` storage read and is subject to the same rules:

#. The read resolves against the state the executing transaction was fixed to
   by consensus, not against whatever a node's chain tip happens to be. Every
   validator of the transaction therefore reads the same octets, whichever
   :term:`Host` serves them.
#. ``f`` (finalized) reads the state at the last finalized block; ``a``
   (accepted, the default) reads the accepted state — the same view a
   read-only :ref:`gvm-def-gl-call-call-contract` child observes by default
   (see :ref:`contract-execution-flow`). Neither view includes the executing
   transaction's own uncommitted writes, so a contract cannot deploy code and
   load it as a runner in the same transaction.
#. The choice between the two is part of the id string and thus part of the
   runner graph, never a node-local default.

A resolution that finds no code, or code that is not a valid :term:`runner`,
fails like any other malformed runner rather than falling back to another view.

Hash Format
~~~~~~~~~~~

``hash`` is a 256-bit hash of the runner's contents, encoded with
:doc:`../04-contract-interface/06-gvm32` (GVM32, a lowercase Crockford
Base32). This keeps it free of filesystem-illegal characters and
case-insensitive.

The algorithm differs per id form:

- ``<human-readable-id>:<hash>`` — SHA-256, matching the hash the runner is
  packaged and distributed under.
- ``custom:<hash>`` — SHA3-256 of the registered blob.

Runner Layout
-------------

For any of the layouts a file list is constructed. Each entry name:

- Must not be empty or start with ``/``
- Must use ``/`` as the path separator; backslashes are forbidden
- Must not contain empty, ``.`` or ``..`` path components
- Must not end with ``/`` unless the entry is a directory

Each file also has contents as a raw byte slice

1. ZIP Archive
~~~~~~~~~~~~~~

Used if runner bytes represent a ZIP archive

- If successful, extracts the archive contents and processes it as a structured :term:`runner` package
- This format supports complex :term:`runners <Runner>` with multiple files, dependencies, and configuration
- Only allowed compression is ``stored`` (no compression), declared by both the central directory and the entry's local header
- A stored entry's compressed and uncompressed sizes must be equal
- A stored file's contents must match its declared CRC-32
- The local header must agree with the central directory on the entry name, and,
  unless the entry has a data descriptor (general-purpose flag bit 3), on the
  CRC-32 and the sizes. The name is compared as raw bytes and must be valid
  UTF-8, so an entry no two readers would decode alike is rejected rather than
  silently resolved
- Encrypted entries (general-purpose flag bit 0) are rejected
- An entry ending with ``/`` is a directory; its size and CRC-32 must be 0, and it is omitted from the file list
- If several entries share a name, the file list holds the last of them

2. Raw WASM
~~~~~~~~~~~

Used if runner bytes represent a wasm file (magic matches)

Creates a minimal :term:`runner` configuration

.. code-block::

    version = v0.1.0
    runner.json = { "StartWasm": "file" }
    file = # source bytes

3. Text-based
~~~~~~~~~~~~~

Used if neither of previous worked. Must be a valid utf-8 encoded string

Comment Header Format
^^^^^^^^^^^^^^^^^^^^^

The contract source code must begin with comment lines using one of the supported comment syntaxes:

- ``//`` (C-style comments)
- ``#`` (Shell/Python-style comments)
- ``--`` (SQL/Lua/Haskell-style comments)

The comment header consists of:

#. **Version Line** (first comment line): Must start with ``v`` followed by version information
#. **:term:`Runner` Configuration** (subsequent comment lines): JSON configuration for the :term:`runner`

Resulting structure
^^^^^^^^^^^^^^^^^^^

.. code-block::

    version = # first line if started with version, else default
    runner.json = # consequent comment lines with removed comment prefix. All whitespaces are kept as-is
    file = # source bytes

Example
^^^^^^^

.. code-block:: python

   # v1.0.0
   # {
   #   "Depends": "python:latest",
   #   "StartWasm": "python.wasm"
   # }

   exit(30)

``version`` file
----------------
This file must contain a single line with the version of ``genvm`` in ``v<major>.<minor>.<patch>`` format.

If this file is not present, the default version is used.

``runner.json`` File
--------------------

The ``runner.json`` file defines a recursive structure of initialization actions that configure the execution environment for a contract.

Schema is available in :doc:`../appendix/runner-schema`\.

It must be a valid JSON object with described below structure

Each action object accepts exactly the fields shown below. The top-level object may also contain a string ``$schema``
annotation

``Seq``, ``When`` and ``With`` nest actions. Nesting deeper than
:ref:`gvm-def-consts-value-runner-limits-init-action-depth`, or a NUL octet in
any action string, is rejected with
:ref:`gvm-def-str-trie-value-vm-error-invalid-contract-runner-malformed`\.

AddEnv
~~~~~~

Adds an environment variable to the GenVM environment with variable interpolation support using ``${}`` syntax.

The name is at most :ref:`gvm-def-consts-value-runner-limits-env-name-len`
octets and consists of ASCII characters other than ``=``, whitespace and control
characters. The value after interpolation is at most
:ref:`gvm-def-consts-value-runner-limits-env-value-len` octets. Violating either
is rejected with
:ref:`gvm-def-str-trie-value-vm-error-invalid-contract-runner-malformed`\.

The interpolated value's length in octets is charged as
:ref:`gvm-def-ram-consumption`; exceeding the budget results in
:ref:`gvm-def-str-trie-value-vm-error-out-of-memory`\.

Example
^^^^^^^

.. code-block:: json

   {
       "AddEnv": {
           "name": "DEBUG",
           "val": "true"
       }
   }

MapFile
~~~~~~~

Maps files or directories from an archive to specific paths in the GenVM filesystem.

Properties
^^^^^^^^^^

- ``file`` (string): Path within the archive. If ending with ``/``, recursively maps all files in the directory
- ``to`` (string): Absolute destination path in the GenVM filesystem

Example
^^^^^^^

.. code-block:: json

   {
       "MapFile": {
           "file": "config/",
           "to": "/etc/myapp/"
       }
   }

Creating a single file mapping implies :ref:`gvm-def-ram-consumption` of

#. :ref:`gvm-def-consts-value-memory-limiter-consts-file-mapping`\.
#. file name length in octets

SetArgs
~~~~~~~

Sets process arguments for the GenVM environment.

**Type:** Array of strings

Example
^^^^^^^

.. code-block:: json

   {
       "SetArgs": ["exe-name", "--verbose", "--config", "/path/to/config"]
   }

LinkWasm
~~~~~~~~

Links a WebAssembly file to make it available in GenVM.

**Type:** String (path to WebAssembly file)

.. code-block:: json

   {
       "LinkWasm": "path/in/arch/to/module.wasm"
   }

If function _initialize is present, it will be called immediately after linking.

.. _gvm-def-start-wasm:

StartWasm
~~~~~~~~~

Starts a specific WebAssembly file in GenVM.

**Type:** String (path to WebAssembly file)

Example
^^^^^^^

.. code-block:: json

   {
       "StartWasm": "path/in/arch/to/module.wasm"
   }

This is a terminal action in the runner configuration. It results in linking the module and calling ``_start`` function.

Depends
~~~~~~~

Specifies a dependency on another :term:`runner` by its ID and hash.

Example
^^^^^^^

.. code-block:: json

   {
       "Depends": "cpython:123"
   }

Dependencies are processed only once, for the first request

Seq
~~~

Executes a sequence of initialization actions.

.. code-block:: json

   {
       "Seq": [
           { "SetArgs": ["exe-name", "--verbose", "--config", "/path/to/config"] },
           { "StartWasm": "path/in/arch/to/module.wasm" }
       ]
   }

When
~~~~

Conditionally executes an action based on WebAssembly execution mode.

Properties
^^^^^^^^^^

- ``cond``: WebAssembly mode, either ``det`` (deterministic) or ``nondet`` (non-deterministic)
- ``action``: Action to execute when condition is met

Example
^^^^^^^

.. code-block:: json

   {
       "When": {
           "cond": "det",
           "action": { "AddEnv": {"name": "MODE", "val": "deterministic"} }
       }
   }

With
~~~~

Sets a :term:`runner` as current without executing its action, useful for reusing files or creating :term:`runner` locks.

Example
^^^^^^^

.. code-block:: json

   {
       "With": {
           "runner": "base-environment",
           "action": { "MapFile": {"file": "patched.foo", "to": "foo" } }
       }
   }

Startup
-------

Runner actions are executed left-recursively, until :ref:`gvm-def-start-wasm` is reached.
If it was not reached, it will result in a :ref:`gvm-def-vm-error` with
``invalid_contract runner malformed`` code.

Loading a :term:`runner` goes through a single **load action**, defined per
:term:`sub-VM`. Each :term:`sub-VM` owns a **loaded-runner set**: the runner
ids it has already loaded. The load action for an id is:

- if the id is already in the :term:`sub-VM`'s loaded set, nothing is charged;
- otherwise :ref:`gvm-def-consts-value-memory-limiter-consts-runner-load-cost` plus the runner's size in octets is charged as
  :ref:`gvm-def-ram-consumption` against the :term:`sub-VM`'s RAM budget, and
  the id is then added to the loaded set.

Whether the executor has the archive cached internally is not observable: the
charge depends only on the :term:`sub-VM`'s own load history, never on cache
state. The same runner loaded by different :term:`sub-VM` instances is charged
once per :term:`sub-VM`. :ref:`gvm-def-det-mode` and
:ref:`gvm-def-non-det-mode` have separate RAM budgets and separate loaded sets.

A load action occurs when:

- spawning the :term:`sub-VM`'s main (entry-point) runner;
- resolving a ``Depends`` or ``With`` action in a ``runner.json``;
- executing the ``MapFile`` ``gl_call``;
- registering a runner via the ``RegisterRunner`` ``gl_call``;
- receiving a custom-runner grant at :term:`sub-VM` creation
  (see :ref:`gvm-meta-property-custom-runners`).

For a ``chain:`` runner the size is the length of the code blob read from
storage. A ``chain:`` load costs the same as any other load of that size —
there is no doubled charge and no separate fee component.

.. _gvm-def-custom-runner-visibility:

Custom :term:`Runner` Loading
-----------------------------

A ``custom:<hash>`` id resolves *iff* ``<hash>`` is in the resolving
:term:`sub-VM`'s own loaded set; otherwise loading it fails with a
:ref:`gvm-def-vm-error`. There is no separate registry lookup at resolution — a
:term:`sub-VM` can use exactly the custom runners it has loaded, whether by
registering them itself via the ``RegisterRunner`` ``gl_call`` or by receiving
grants from its parent at creation time
(:ref:`gvm-meta-property-custom-runners`).

Registered content lives while at least one :term:`sub-VM` has it loaded; once
no loaded set holds it, it is freed. Registering the same ``code`` again while
it is still loaded somewhere is deduplicated by hash; re-registering it after it
has been freed re-parses and charges again.
