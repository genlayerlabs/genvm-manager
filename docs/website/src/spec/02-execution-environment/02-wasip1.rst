WASI Preview 1 Implementation
=============================

Overview
--------

GenVM implements WebAssembly System Interface (WASI) Preview 1 to
provide standardized system-level functionality to WebAssembly modules.
The implementation includes modifications for deterministic execution
required by blockchain consensus while maintaining compatibility with
standard WASI applications.

WASI Preview 1 Foundation
-------------------------

Standard Interface
~~~~~~~~~~~~~~~~~~

-  **System Calls**:

   -  File system operations (open, read, write, close)
   -  Process management (exit, args, environment)
   -  Time and clock access
   -  Random number generation
   -  Socket and network operations

-  **Data Types**:

   -  Standard WASI types for file descriptors, time, and sizes
   -  Cross-platform compatibility abstractions
   -  Error code standardization
   -  Memory layout specifications

Deterministic Modifications
---------------------------

Time and Randomness Control
~~~~~~~~~~~~~~~~~~~~~~~~~~~

-  **Controlled Time Access**:

   -  Deterministic time functions for consensus requirements
   -  Time zone and locale standardization

-  **Deterministic Randomness**:

   -  Deterministic randomness for deterministic operations
   -  Cryptographically secure random number generation in non-deterministic mode

Regular system interface
~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-vfs:

Virtual File System
^^^^^^^^^^^^^^^^^^^

-  Isolated file system namespace per contract execution
-  Memory-based file system for deterministic behavior
-  Read-only access to runtime libraries and dependencies
-  Controlled file system state for reproducible execution

Environment Variables
^^^^^^^^^^^^^^^^^^^^^

-  Controlled environment variable access
-  Deterministic environment setup
-  Security filtering of sensitive variables
-  Standardized locale and language settings

Command Line Arguments
^^^^^^^^^^^^^^^^^^^^^^

-  Controlled argument passing to WebAssembly modules
-  Deterministic argument parsing and validation
-  Security filtering of dangerous arguments
-  Standardized argument format and encoding

WASI Specification Compliance
-----------------------------

-  **Interface Compatibility**:

   -  Full compatibility with WASI Preview 1 specification
   -  Standard function signatures and behavior
   -  Compatible error handling and reporting
   -  Consistent data type definitions

-  **Ecosystem Integration**:

   -  Support for WASI-targeting compilers
   -  Compatibility with existing WASI libraries
   -  Tool chain integration and support
   -  Community standard compliance

Always Erroring Operations
--------------------------

Fail with ``Acces`` error code:

- ``sock_accept``
- ``sock_recv``
- ``sock_send``
- ``sock_shutdown``

Fail with ``Rofs`` error code:

- ``fd_allocate``
- ``fd_fdstat_set_flags``
- ``fd_fdstat_set_rights``
- ``fd_filestat_set_size``
- ``fd_filestat_set_times``
- ``path_create_directory``
- ``path_filestat_set_times``
- ``path_link``
- ``path_remove_directory``
- ``path_rename``
- ``path_symlink``
- ``path_unlink_file``

Fail with ``Badf`` error code:

- ``path_readlink``

Fail with ``Notsup`` error code:

- ``poll_oneoff``
- ``proc_raise``
- ``sched_yield``
- ``fd_pwrite``
- ``fd_renumber``


Functions
---------

``random_get`` Function
~~~~~~~~~~~~~~~~~~~~~~~

Deterministic mode: mt19937 that is initialized from ``sha3-256`` of the VM's stdin
(the calldata-encoded message data). The 32-byte digest is consumed as 8
little-endian ``u32`` words that form the mt19937 slice seed. The seed is fixed at
VM construction time and is fully determined by the VM inputs, so two deterministic
runs with identical stdin see the same ``random_get`` stream, while differing inputs
produce diverging streams. Non-deterministic randomness MUST be obtained via
:ref:`gvm-def-non-det-mode` calls.

Non-deterministic mode: a cryptographically secure random number generator. If the
secure source is unavailable or fails, ``random_get`` MUST fail with ``errno::io``
instead of falling back to a predictable stream (pseudo-random or zeroed output). A
caller that wants a graceful fallback must handle that error explicitly.

``proc_exit`` Function
~~~~~~~~~~~~~~~~~~~~~~

#. ``proc_exit(0)`` is equivalent to :ref:`gvm-def-return` of ``null`` value.
#. ``proc_exit(x)`` where :math:`x \neq 0` results in :ref:`gvm-def-vm-error`

.. _gvm-def-vfs-path-resolution:

Path Resolution
~~~~~~~~~~~~~~~

``path_*`` functions take a directory :term:`FD` and a path. Resolution is
purely lexical and identical in both modes:

#. The directory descriptor's own path and the supplied path are concatenated
   and split on ``/``. Empty and ``.`` components are dropped, a ``..``
   component pops the preceding one, and a ``..`` that would escape the root is
   dropped. There are no symlinks, so ``dirflags`` never changes the result.
#. The resulting absolute path must lie within a preopened subtree, otherwise
   the call fails with ``Notcapable``.
#. The path is then walked through the :ref:`gvm-def-vfs`: a missing component
   fails with ``Noent``, and a component that resolves to a file while more
   components remain fails with ``Badf``.

A descriptor that is not a directory fails with ``Badf`` before any of this.

File Metadata
~~~~~~~~~~~~~

The :ref:`gvm-def-vfs` stores no metadata, so every ``Filestat`` reports
``dev``, ``ino``, ``atim``, ``mtim`` and ``ctim`` as ``0``; ``size`` is the
file's length in octets and ``0`` for a directory, and ``nlink`` is ``1``.
Timestamps are **not** derived from the transaction timestamp — a file has no
modification time at all.

Operations represented in a target type's supported base mask require the
corresponding right on a descriptor returned by ``path_open`` and fail with
``Notcapable`` when it is absent. Supported masks are defined by
:ref:`gvm-def-wasi-descriptor-rights`.

``path_open`` Function
~~~~~~~~~~~~~~~~~~~~~~

#. The path is resolved as in `Path Resolution`_.
#. The directory descriptor requires the ``path_open`` base right, otherwise
   the call fails with ``Notcapable``.
#. If ``oflags::directory`` targets a regular file, the call fails with
   ``Notdir``. The :ref:`gvm-def-vfs` is read-only, so ``creat``, ``excl`` and
   ``trunc`` never take effect — opening a non-existent path fails with
   ``Noent`` rather than creating it. ``fdflags`` are ignored.
#. The new descriptor's base and inheriting rights are limited by the
   corresponding requested rights and the parent descriptor's inheriting
   rights. Unsupported target-type rights are then removed as defined in
   :ref:`gvm-def-wasi-descriptor-rights`.
#. On success a fresh descriptor is allocated (see
   :ref:`gvm-def-fd-allocation`) that refers to the resolved file or directory.
   A file descriptor starts at offset ``0``.
#. A failed call allocates no descriptor and consumes no descriptor
   :ref:`gvm-def-ram-consumption`.

``path_filestat_get`` Function
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Resolves the path as in `Path Resolution`_ and returns the ``Filestat``
described in `File Metadata`_ with ``filetype`` ``regular_file`` or
``directory``.

``fd_readdir`` Function
~~~~~~~~~~~~~~~~~~~~~~~

After the rights check, a descriptor that is not a directory fails with
``Badf``.

The entry sequence of a directory is fixed and does not depend on how the
:ref:`gvm-def-vfs` was populated:

#. ``.`` — filetype ``directory``
#. ``..`` — filetype ``directory``, and refers to the root itself in the root
   directory
#. the directory's children in ascending order of their names, compared as raw
   octet sequences; a child's filetype is ``directory`` or ``regular_file``

``d_ino`` is ``0`` for every entry, ``d_namlen`` is the name's length in
octets, and ``d_next`` is the cookie of the following entry — cookies number
the sequence above from ``1``. The ``cookie`` argument skips that many leading
entries; a cookie past the end yields no entries.

Entries are written as a ``Dirent`` immediately followed by the name, until the
buffer is exhausted. A truncated entry is not backed out: the last ``Dirent``
or name may be cut mid-way, and the call then returns ``buf_len`` to signal
that the caller must retry with a larger buffer. Otherwise it returns the
number of octets written.

``fd_tell`` Function
~~~~~~~~~~~~~~~~~~~~

Returns the descriptor's current offset. ``stdout``/``stderr`` fail with
``Spipe``, a directory with ``Notsup``.

``fd_datasync`` Function
~~~~~~~~~~~~~~~~~~~~~~~~

Does nothing and returns success after the rights check.

``fd_sync`` Function
~~~~~~~~~~~~~~~~~~~~

Does nothing and returns success after the rights check.

``fd_seek`` Function
~~~~~~~~~~~~~~~~~~~~

Moves a file descriptor's offset and returns the new value.

#. ``set`` and ``cur`` clamp the result into :math:`[0, \texttt{size}]` instead
   of failing: a negative target becomes ``0`` and a target beyond the end
   becomes the file's size.
#. ``end`` is not supported and fails with ``Notsup``.
#. ``stdout``/``stderr`` fail with ``Spipe``, a directory with ``Notsup``.

``fd_prestat_dir_name`` Function
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The only preopened directory is the VFS root. This function writes its
guest-visible name, ``/``. ``fd_prestat_get`` returns the ``Prestat`` whose
``pr_name_len`` is therefore ``1``. A buffer shorter than that fails with
``Overflow``; every descriptor other than the root preopen, including a
directory returned by ``path_open``, fails with ``Badf``.

``fd_prestat_get`` Function
~~~~~~~~~~~~~~~~~~~~~~~~~~~

#. For the root preopen, returns a ``Dir`` whose ``pr_name_len`` is ``1``, the
   length of its guest-visible name ``/``. This is required for libc preopen
   discovery.
#. For any other descriptor (``stdin``/``stdout``/``stderr``, a regular file, or a
   directory returned by ``path_open``, or a non-existent descriptor) returns
   ``Badf``.

``fd_write`` Function
~~~~~~~~~~~~~~~~~~~~~

After the rights check, only ``stdout`` and ``stderr`` are writable; a file or
directory descriptor fails with ``Rofs``. The written octets are diagnostic
output: they are not part of the :ref:`gvm-def-vm-result` and are not covered
by the :ref:`gvm-def-execution-hash`.

The call reports every supplied octet as written and never fails on the
underlying stream, so a contract cannot observe whether the :term:`Host`
accepted, buffered or discarded the output.

``fd_read`` Function
~~~~~~~~~~~~~~~~~~~~

Reads a file descriptor's contents into the ``iovec``\ s in order, starting at
the descriptor's offset and advancing it by the number of octets read. Reading
at or past the end of the file yields ``0`` octets rather than an error.
``stdout``/``stderr`` fail with ``Acces``, a directory with ``Isdir``.

``fd_pread`` Function
~~~~~~~~~~~~~~~~~~~~~

As ``fd_read``, except that it starts at the explicit ``offset`` and leaves the
descriptor's own offset unchanged.

``fd_filestat_get`` Function
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Returns the ``Filestat`` described in `File Metadata`_ with ``nlink`` ``1`` and
``filetype``:

- ``regular_file`` for a file descriptor, whose ``size`` is the file's length
- ``directory`` for a directory descriptor
- ``character_device`` for ``stdout``/``stderr``

.. _gvm-def-wasi-descriptor-rights:

``fd_fdstat_get`` Function
~~~~~~~~~~~~~~~~~~~~~~~~~~

``fs_flags`` is always empty — no descriptor is appending, non-blocking or
synchronous. The maximum read-only rights are:

- A regular-file descriptor has the read, seek, tell, advise, sync, datasync
  and fd-filestat-get base rights, and no inheriting rights
- A directory descriptor has the ``path_open``, readdir, path-filestat-get and
  fd-filestat-get base rights; its supported inheriting rights are the union of
  the regular-file and directory base rights
- ``stdout``/``stderr`` have ``unknown`` filetype and the write right in both
  masks

The root preopen receives the full directory masks. For a descriptor returned
by ``path_open``, both masks are further limited as described in that function.

``fd_close`` Function
~~~~~~~~~~~~~~~~~~~~~

Deallocates the descriptor (see :ref:`gvm-def-fd-allocation`) and releases the
:ref:`gvm-def-ram-consumption` charged for its contents, if any. An unknown
descriptor fails with ``Badf``.

``fd_advise`` Function
~~~~~~~~~~~~~~~~~~~~~~

Does nothing and returns success after the rights check.

``clock_time_get`` Function
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Returns transaction unix timestamp in **both** modes, **regardless of the requested
clock id** (``realtime``, ``monotonic``, ``process_cputime_id`` and ``thread_cputime_id``
all return the same value). Standard WASI distinguishes these clocks; GenVM does not,
because non-determinism would otherwise leak through the monotonic clock in
:ref:`gvm-def-det-mode`.

``clock_res_get`` Function
~~~~~~~~~~~~~~~~~~~~~~~~~~

Always returns ``1`` (one nanosecond) for every clock id. The actual granularity of
``clock_time_get`` is bounded by the granularity of the host-provided transaction
timestamp.

``environ_sizes_get`` Function
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Reports the size of the host-supplied environment block. The contract cannot mutate it,
and the host fills it from a fixed list per runner; see :doc:`04-runners`. There is no
inheritance from the operating-system environment.

``environ_get`` Function
~~~~~~~~~~~~~~~~~~~~~~~~

Copies the host-supplied environment block into guest memory.

``args_sizes_get`` Function
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Reports the size of the host-supplied argv. The first argument is the runner's entry
point (e.g. the contract source path inside the VFS); subsequent arguments are
provided by the runner, never by an external caller.

``args_get`` Function
~~~~~~~~~~~~~~~~~~~~~

Copies the host-supplied argv into guest memory.

Virtual File System
-------------------

Initial State
~~~~~~~~~~~~~

- :term:`FD` 0 is a file that contains :ref:`Calldata Encoded <gvm-def-calldata-encoding>` extended message
- :term:`FD` 1 is ``stdout``
- :term:`FD` 2 is ``stderr``
- :term:`FD` 3 is directory ``/`` (file system root)

.. _gvm-def-fd-allocation:

:ref:`gvm-def-det-mode` :term:`FD` Allocation and Deallocation
--------------------------------------------------------------

Pseudocode
~~~~~~~~~~

.. code-block::

   allocate() → FD:
      if free_pool.is_empty():
         consume_ram()
         next_id += 1
         allocated.insert(next_id)
         return next_id
      else:
         fd = free_pool.pop()
         allocated.insert(fd)
         return fd

   deallocate(fd: FD):
      require: fd ∈ allocated
      allocated.remove(fd)
      free_pool.push(fd)

Allocating a new :term:`FD` implies :ref:`gvm-def-ram-consumption` of :ref:`gvm-def-consts-value-memory-limiter-consts-fd-allocation`\.

Invariants
~~~~~~~~~~

#. :math:`\texttt{allocated}\cap\texttt{free_pool} = \emptyset`
#. :math:`\texttt{next_id} \ge \operatorname{max}(\texttt{allocated}\cup\texttt{free_pool})`
#. All returned descriptors are unique until deallocated

.. warning::

   :ref:`gvm-def-non-det-mode` is not obligated to follow this pattern
