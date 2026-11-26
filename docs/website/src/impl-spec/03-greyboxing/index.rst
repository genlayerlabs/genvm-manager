Greyboxing Documentation
========================

.. toctree::
   :maxdepth: 2

It refers to the technique of preventing attacks on LLMs. Implementing it is a responsibility of every node,
as bundled presets can be attacked.

Greyboxing can be achieved by a few methods:

#. Using different llms, potentially selected based on the request itself
#. Randomizing llm calling parameters
#. Modifying prompts

The LLM :term:`Module` provides greyboxing capabilities via lua scripting.

Retrieving Data from :term:`Host`
---------------------------------

Host can provide additional data to the :term:`Module` to help it make decisions,
only transaction id and node address are required, as they are required for signing requests.

Token Charges
-------------

The default Lua policy charges 1/4 time unit per 1,000 provider-reported total
tokens, using the host's ``gas_data.genPerTimeUnit`` price. A model can override
the rate with ``meta.time_units_per_1k_tokens``, a non-negative rational string
such as ``"1/2"`` or ``"0"``. Invalid rates fail before calling that provider

The charge is ``ceil(total_tokens * genPerTimeUnit * rate / 1000)`` in GEN-wei,
computed with exact rational arithmetic. Missing or zero ``genPerTimeUnit``
keeps the charge at zero

Default Policy
--------------

The default Lua script routes through the ``llm_policy`` engine. The chain and
mutation keys below are optional; a config without them keeps the
``meta.priority`` order and the prompt's own text, images and temperature

- **Chains:** once any model sets ``meta.greybox = { text = N, image = M }``, text
  prompts try the ``text`` chain and image prompts the ``image`` chain, lower
  number first. Equal numbers are ordered by
  ``sha256("<node address>/<provider>/<model>")``: fixed for one node, different
  across nodes. Models outside the
  prompt's chain are never tried; an empty chain fails with
  ``NO_PROVIDER_FOR_PROMPT``. Other keys, or neither key, fail when the script loads
- **Mutation:** the module-level ``meta.mutate`` is an ``llm_policy`` declarative
  mutate spec, for example::

     meta:
       mutate:
         pipe:
           - filter_text: [NFKC]
           - filter_image: [{ GuassianNoise: 1.0 }]
           - jitter: { temperature: 0.3 }
           - clamp: { temperature: 1.0 }

  ``filter_text`` covers the system and user message. ``filter_image`` accepts
  ``GuassianNoise`` and ``JPEG``, and re-encodes every image as PNG; an image it
  cannot decode fails with ``INVALID_IMAGE``. ``jitter``, ``set_param`` and
  ``clamp`` accept ``temperature`` only; the result never goes below 0, and the
  upper bound is the operator's ``clamp``, since providers differ (Anthropic
  stops at 1). All attempts of one prompt get the same mutation. Any other step
  or parameter fails when the script loads
- **Seed:** every prompt gets a fresh random sampling seed, logged as
  ``llm call seed``. It seeds ``jitter`` and is sent to OpenAI-compatible and
  Ollama backends

Current Built-in Filters
------------------------

To simplify implementation, we provide a set of built-in filters
that can be used from the script

Text
^^^^

- Zero width character removal
- Whitespace normalization
- Unicode normalization

Image
^^^^^

- Unsharpen
- GuassianNoise
- JPEG reconversion

Example Usage
-------------


.. code-block:: lua

  args.prompt = lib.rs.filter_text(args.prompt, {
    'NFKC',
    'RmZeroWidth',
    'NormalizeWS'
  })

  args.images[0] = lib.rs.filter_image(args.images[0], {
    { Unsharpen = { 2.0, 4.0 } },
    { GuassianNoise = 0.05 },
    { JPEG = 0.8 }
  })

Sandbox and Execution Model
---------------------------

Each :term:`Module` runs an embedded Lua 5.4 interpreter. The host owns the VM pool
(``vm_count`` in the module config); a single ``ctx`` object lives for the duration of
one GenVM invocation and is the only state that may be relied upon across calls.

The script does **not** see any of Lua's I/O surface: ``io``, ``os.execute``, ``debug``,
``package.loadlib`` and the C ``require`` loader are not exposed. The only effects
available are the ``lib.rs.*`` host functions (HTTP, base64, JSON, image/text filters,
sleep, signing, ``user_error``) and, in the LLM module, ``llm.rs.*`` plus the
``lsqlite3`` binding rooted under ``data_dir`` (declared in the module config).

Global state across invocations is *not* guaranteed: two GenVM runs may land on
different VMs in the pool, or on the same VM in either order, and the host gives no
ordering guarantee. Cross-invocation state belongs in the sqlite database or must be
re-derived from the host-provided arguments on every call.

Entry Points
------------

The host calls Lua functions by name.

LLM module:

- ``Setup(ctx)`` / ``Teardown(ctx)`` — per-session lifecycle hooks (called when a
  GenVM session is opened/closed; may persist data into ``ctx``).
- ``ExecPrompt(ctx, args, remaining_gen)`` — handles ``ExecPrompt`` calls; receives
  the resolved prompt and must return the provider result or raise via
  ``lib.rs.user_error``.
- ``ExecPromptTemplate(ctx, args, remaining_gen)`` — handles ``EqComparative`` /
  ``EqNonComparativeLeader`` / ``EqNonComparativeValidator`` templates. The template
  name is in ``args.template``; the remaining keys are the template's named slots
  (e.g. ``leader_answer``, ``validator_answer``, ``principle`` for ``EqComparative``).
  The default script runs every string slot through the same text filters
  ``ExecPrompt`` applies to its prompt (NFKC, zero-width removal, whitespace
  normalization) before filling the template.

Web module:

- ``Render(ctx, payload)`` — handles ``WebRender``; ``payload.mode`` is one of
  ``"text" | "html" | "screenshot"`` and the function must return
  ``{ text = ... }`` or ``{ image = ... }`` accordingly.
- ``Request(ctx, payload)`` — handles ``WebRequest``; must validate the URL against
  ``web.allowed_tld`` / ``web.always_allow_hosts`` before issuing the outbound request.

The third argument ``remaining_gen`` (LLM only) is the remaining generation budget
in wei as a rational; scripts SHOULD reject requests they cannot finish within the
budget rather than overspending.

Error Propagation
-----------------

The script signals a user-visible failure by calling ``lib.rs.user_error`` with a
``ModuleError``::

   {
     causes = { "WEBPAGE_LOAD_FAILED", ... },  -- string tags, joined into the cause chain
     ctx    = { ... },                         -- arbitrary key/value context
     fatal  = true | false,                    -- non-fatal errors may be retried
   }

A ``fatal = false`` error is reported as a recoverable failure: the host may select a
different backend (LLM) or surface the error to the contract while preserving budgets.
``fatal = true`` aborts the surrounding GenVM call with a :ref:`gvm-def-user-error`.

An uncaught Lua error (``error(...)``, type error, sandbox violation) is treated as
:ref:`gvm-def-internal-error` and ends the run; scripts MUST wrap fallible host calls
in ``pcall`` if they want to convert failures into ``user_error``\s instead.

Resource Limits
---------------

The Lua VM itself runs without fuel. Constraints are enforced indirectly:

- HTTP and signing requests go through ``lib.rs.request``, which the host caps with
  the per-call ``response_body_max_size`` and a hard timeout derived from the
  remaining session budget (see ``compute_timeout`` in the default LLM script).
- LLM provider calls debit ``ctx.policy.spent_gen_wei`` against the contract's
  ``remaining_gen`` budget. When ``stop_on_spent`` is reached, ``ctx.policy.exhausted``
  is set and further attempts SHOULD abort.
- Per-session sqlite state is bounded only by ``data_dir`` disk space; greyboxing
  scripts MUST garbage-collect on their own.

Template Contracts
------------------

The three template entry points feed into
:external+executor:doc:`spec/02-execution-environment/03-wasi_genlayer_sdk/02-gl_call`
``ExecPromptTemplate``. Their wire-level payload is fixed by the spec; the template
body strings interpolated into the LLM prompt come from the ``prompt_templates`` block
of the LLM module config and MUST contain the documented ``#{...}`` placeholders:

- ``EqComparative`` — ``#{leader_answer}``, ``#{validator_answer}``, ``#{principle}``;
  the script MUST return a boolean response (``true`` means the validator agrees with
  the leader).
- ``EqNonComparativeLeader`` — ``#{task}``, ``#{criteria}``, ``#{input}``; returns a
  text response (the leader's answer).
- ``EqNonComparativeValidator`` — ``#{task}``, ``#{criteria}``, ``#{input}``,
  ``#{output}``; returns a boolean response indicating whether ``output`` satisfies
  ``criteria`` for ``input``.

Missing placeholders are a config error and surface as :ref:`gvm-def-internal-error`
at module startup.
