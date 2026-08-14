Response Schemas
================

Operations that return data via file descriptor use specific encoding formats
based on operation type.

.. _gvm-def-subvm-result-encoding:

Sub-VM Result Encoding
----------------------

Operations that spawn sub-VMs (:ref:`gvm-def-gl-call-call-contract`,
:ref:`gvm-def-gl-call-run-nondet`,
:ref:`gvm-def-gl-call-sandbox`) return
results through a file descriptor with the following binary format:

.. code-block::

   [result_code: u8][data: bytes]

Where ``result_code`` is a :ref:`gvm-def-enum-result-code`:

- :ref:`gvm-def-enum-value-result-code-return` (``0``): Successful execution
- :ref:`gvm-def-enum-value-result-code-user-error` (``1``): Contract-initiated error
- :ref:`gvm-def-enum-value-result-code-vm-error` (``2``): VM-level error

The ``data`` portion depends on the result code:

- **return**: :ref:`Calldata Encoded <gvm-def-calldata-encoding>` return value
- **user_error**: :ref:`Calldata Encoded <gvm-def-calldata-encoding>` error value
- **vm_error**: UTF-8 encoded :ref:`gvm-def-str-trie-vm-error` string

.. _gvm-def-proposed-result-validity:

Validity Of A Proposed Result
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A result the executor did not compute itself — the leader-proposed
non-deterministic result consumed in :ref:`gvm-def-sync-mode` and
:ref:`gvm-def-validator-mode` — is accepted only if it satisfies all of the
following. A rejected proposal never traps and never bypasses the comparison
stage; it is replaced by a derived :ref:`gvm-def-vm-error` and handed to the
comparison as if it had been proposed.

#. The buffer is non-empty. An absent or empty proposal yields
   :ref:`gvm-def-str-trie-value-vm-error-leader-fault-nondet-output-absent`.
#. ``result_code`` is one of
   :ref:`gvm-def-enum-value-result-code-return`,
   :ref:`gvm-def-enum-value-result-code-user-error` or
   :ref:`gvm-def-enum-value-result-code-vm-error`; any other byte yields
   :ref:`gvm-def-str-trie-value-vm-error-leader-fault-nondet-output-malformed`.
#. For **return** and **user_error**, ``data`` is valid
   :ref:`gvm-def-calldata-encoding` with no trailing bytes; otherwise
   :ref:`gvm-def-str-trie-value-vm-error-leader-fault-nondet-output-malformed`.
#. For **vm_error**, ``data`` is UTF-8, carries no ``" # "`` detail, and names a
   :ref:`gvm-def-str-trie-vm-error` path, including the canonical spelling of
   any parameter. Codes outside the trie yield
   :ref:`gvm-def-str-trie-value-vm-error-leader-fault-nondet-output-malformed`.

An accepted result is preserved **byte for byte**: validation never re-encodes,
so every node hashes the value that was proposed.

.. _gvm-def-derived-outcome-namespace:

Derived-Outcome Namespace
~~~~~~~~~~~~~~~~~~~~~~~~~

Following :ref:`gvm-def-vm-error` codes are derived by the consuming executor
rather than proposed:

#. :ref:`gvm-def-str-trie-value-vm-error-leader-fault-nondet-output-absent`
#. :ref:`gvm-def-str-trie-value-vm-error-leader-fault-nondet-output-malformed`
#. :ref:`gvm-def-str-trie-value-vm-error-leader-fault-nondet-output-uses-this-error`
#. :ref:`gvm-def-str-trie-value-vm-error-leader-fault-nondet-output-extra`

A proposed code that equals, or extends at a space boundary,
``leader_fault nondet_output`` is replaced by
:ref:`gvm-def-str-trie-value-vm-error-leader-fault-nondet-output-uses-this-error`
whose parameter is the first 6 characters of the
:doc:`../../04-contract-interface/06-gvm32` encoding of ``sha3_256`` of the
proposed code. A proposal that is its own replacement maps to the parameter
``fix_point``, which cannot collide with a derived parameter because it is 9
characters long and a derived one is always 6.

This check runs **before** the trie-validity check, so proposing a derived code
verbatim can never produce output byte-equal to the proposal. Distinct proposals
may share a parameter; the mapping is deterministic and the value carries no
meaning beyond identifying the proposal as rejected.

.. _gvm-def-module-result-encoding:

Module Result Encoding
----------------------

Operations that invoke external modules (``WebRender``, ``WebRequest``, ``ExecPrompt``,
``ExecPromptTemplate``) return results as :ref:`Calldata Encoded <gvm-def-calldata-encoding>`
data with a success/error wrapper:

.. code-block::

   // On success
   {
     "ok": <result>
   }

   // On error
   {
     "error": <error_details>
   }

The ``<result>`` structure depends on the operation:

``WebRender`` Response
~~~~~~~~~~~~~~~~~~~~~~

Returns one of the following based on render mode:

.. code-block::

   // For mode "text"
   {"ok": {"text": String}}

   // For mode "html" (returns HTTP response)
   {"ok": {"response": Response}}

   // For mode "screenshot"
   {"ok": {"image": Bytes}}

Where ``Response`` is:

.. code-block::

   {
     "status": Number,           // HTTP status code (u16)
     "headers": Map,             // String -> Bytes mapping
     "body": Bytes               // Response body
   }

``WebRequest`` Response
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block::

   {
     "ok": {
       "status": Number,         // HTTP status code (u16)
       "headers": Map,           // String -> Bytes mapping of response headers
       "body": Bytes             // Response body
     }
   }

``ExecPrompt`` Response
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block::

   // When response_format is "text"
   {"ok": String}

   // When response_format is "json"
   {"ok": Object}

``ExecPromptTemplate`` Response
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block::

   // For EqComparative template (boolean result)
   {"ok": Bool}

   // For EqNonComparativeValidator template (boolean result)
   {"ok": Bool}

   // For EqNonComparativeLeader template (text result)
   {"ok": String}

Module Error Format
~~~~~~~~~~~~~~~~~~~

When a module operation fails, the error is returned as:

.. code-block::

   {
     "error": <GenericValue>
   }

Where ``<GenericValue>`` may contain error details from the module.
