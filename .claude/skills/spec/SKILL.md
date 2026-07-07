---
name: spec
description: How to write GenVM spec pages (docs/website/src/spec). Use when adding or editing spec sections — brevity, linking constants/errors/terms instead of inlining them, spec vs impl-spec split, and how to verify the build.
---

# Writing spec

The spec (`docs/website/src/spec/`) describes **observable behavior** — what a
contract or validator can detect. How this codebase achieves it goes to
`impl-spec/`. If a sentence starts describing internals (caches, native limits,
threads), either drop it or reduce it to one normative requirement on
implementations.

## Be brief

- One concern per section; a few paragraphs or one list is the right size.
- Enumerate cases with `#.` lists instead of prose walkthroughs; name the
  actors precisely (*caller*/*callee*, leader/validator) and describe each
  case once.
- State what happens, not why the design is good. Rationale goes to ADRs
  (`docs/adr/`).
- Cover the edges (unwinding, host boundary, validation-time rejection) in one
  sentence each — omitting them is sweeping under the rug, but they rarely
  deserve a paragraph.

## Link, don't inline

Never write a literal value or error string in spec text — link the anchor, so
generated pages stay the single source of truth:

- Constants: `:ref:`gvm-def-consts-value-<group>-<name>`` /
  `:ref:`gvm-def-const-<name>`` from `spec/appendix/constants.rst`
  (generated from `executor/codegen/data/public-abi.json`) or
  `constants-pending.rst` (from `public-abi-pending.json`). **Never edit these
  .rst by hand** — edit the JSON and regenerate
  ([genvm-tool.md](../../../docs/contributing/howto/genvm-tool.md)). New
  not-yet-stabilized constants go to the pending JSON.
- Error outcomes: `:ref:`gvm-def-str-trie-value-vm-error-...`` — every "traps
  with" / "rejected with" must link the exact vm_error entry.
- Glossary terms: `:term:`sub-VM`` etc. on first use in a section.
- Other spec pages: `:doc:` relative links instead of restating their content.

## Verify

Build the website ([docs.md](../../../docs/contributing/howto/building/docs.md))
and check for `undefined label` warnings on your pages — a broken `:ref:` is a
silent dead link otherwise. Verify claims against the implementation before
writing them; every behavioral sentence should have a code location you can
point to (but do not link it to the specification).
