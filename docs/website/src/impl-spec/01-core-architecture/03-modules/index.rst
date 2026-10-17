:term:`Module`\s
================

Overview of GenVM's core architectural components and design patterns.

.. toctree::
   :maxdepth: 2

   01-web
   02-llm

Overview
--------

:term:`Module`\s provide GenVM with non-deterministic capabilities that extend
beyond traditional blockchain operations. These modules enable
intelligent contracts to interact with AI services, access web content,
and process real-world data while maintaining consensus through
specialized validation mechanisms.

Module Architecture
-------------------

Design Principles
~~~~~~~~~~~~~~~~~

-  **Isolation**: :term:`Module`\s run in separate processes to prevent
   contamination of deterministic execution
-  **Extensibility**: Lua scripting support
-  **Security**: Controlled access

Communication Protocol
~~~~~~~~~~~~~~~~~~~~~~

-  **WebSocket Interface**:

   -  Asynchronous communication between GenVM and modules
   -  Message serialization using :ref:`gvm-def-calldata-encoding`
   -  Request-response pattern with timeout handling
   -  Connection lifecycle management and reconnection

-  **Message Structure**:

   -  Standardized request/response envelope
   -  Type-safe parameter encoding
   -  Error handling and status reporting

Module Lifecycle
~~~~~~~~~~~~~~~~

-  **Initialization**:

   -  Module process startup
   -  Configuration loading and validation
   -  Initial health check
   -  Binding listening address

-  **Operation**:

   -  Accepting GenVM connections
   -  Request processing and external service interaction
   -  Result validation and transformation
   -  Resource monitoring and usage tracking
   -  Error handling and recovery procedures

-  **Termination**:

   -  Graceful shutdown and resource cleanup

Caller Disconnection
~~~~~~~~~~~~~~~~~~~~

The executor's module stream is monitored during handler initialization,
requests and response writes. EOF or a read error cancels the active operation.
The reader buffers at most one pending request; further pipelining closes the
connection rather than suspending disconnect detection

Each Lua call owns its pending HTTP, provider and sleep futures. Cancellation
drops these operations even when Lua retains the suspended coroutine, closing
pending WebDriver requests so that the sidecar releases its rendering slot.
Teardown runs synchronous cleanup but is dropped if it would wait asynchronously

This differs from the manager's reconnectable WebSocket: disconnecting that
control connection preserves executions for later attachment, as described in
:doc:`../../appendix/manager-socket`
