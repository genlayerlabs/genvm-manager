:term:`Host` Interface Protocol
===============================

Overview
--------

The :term:`Host` Interface defines the communication protocol between GenVM and
the blockchain node. ``genvm run`` receives execution data as calldata and
communicates via a binary protocol over TCP, Unix domain sockets or an inherited
socket file descriptor.

Process Management
------------------

GenVM Execution
~~~~~~~~~~~~~~~

**Launch Parameters**:

- ``--host``: Repeatable host URI, ordered by host index: TCP address,
  ``unix://`` socket path or ``fd://`` inherited socket descriptor
- ``--execution-data``: Calldata-encoded execution data from standard input by
  default, or from a file path or ``fd://`` descriptor

**Process Control**:

- **Termination**: The executor has no internal timeout or graceful-shutdown path and may be killed at any moment with ``SIGKILL`` (see :doc:`04-executor`). Timeouts are enforced by the :term:`Manager`.
- **Crash Detection**: Process exit before sending result indicates a kill/timeout or a crash (a crash should be reported as a bug)

**Node Responsibilities**: Node decides how to receive code and messages from users. GenVM only knows about calldata and message data.

Communication Protocol
----------------------

Pseudocode is available in :doc:`../appendix/host-loop`

Binary Protocol Design
~~~~~~~~~~~~~~~~~~~~~~



Data Types and Results
----------------------

VM Result Codes
~~~~~~~~~~~~~~~

**Result Types**:

- ``Return``: Successful execution
- ``VMError``: VM-produced error that usually can't be handled
- ``UserError``: User-produced error

Result Encoding
~~~~~~~~~~~~~~~

**Non-deterministic Blocks and Sandbox Encoding**:

- 1 byte of result code
- Result data: calldata for ``Return`` and ``UserError``, UTF-8 for ``VMError``

**Top-Level Result Encoding**:

- 1 byte of result code
- Calldata-encoded ``ReportedResult`` map with the result ``kind`` and ``data``,
  ``backtrace`` and ``wasm_store_hashes`` fingerprints, and execution fields;
  see :doc:`../appendix/manager-socket`

**Host Responsibility**: Calculating storage updates, hashes, and state
management (similar to Ethereum's dirty storage override pattern).

Method ID Reference
-------------------

Method IDs are available as JSON in the build system for code
generation.

Error Handling
--------------

- **Protocol Errors**: Most methods return error codes, with ``json/errors/ok`` indicating success
- **Communication Failures**: Socket communication errors indicate protocol violations
- **Process Termination**: Unexpected process exit indicates GenVM crash and should be reported
