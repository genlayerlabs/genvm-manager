---
name: review-ready
description: The bar a GenVM change clears before it is handed back. Use before pushing, opening or updating a PR, or reporting a change finished — and when asked whether something is ready for review.
---

# Review-Ready

Walk [review-ready.md](../../../docs/contributing/howto/review-ready.md) against
the actual diff and shell, not memory.
[pr.md](../../../docs/contributing/howto/pr.md) has the branch model, the panel,
and authority.

## Report Review-Ready, Never Done

Only 2 answers exist:

- **Review-Ready** — evidence per item, including commands and their output
- **Not Review-Ready** — naming the item that fails and the blocker behind it

An unchecked item means **Not Review-Ready**, not a partial pass. Never report
**Done**, which also requires review, cross-repo E2E, merge and release.

## While you work

- Fix defects in your diff without asking
- Report defects outside it without folding them in or dropping them
- Raise concerns about the requirement before building on it

## Adapt it

With an escaped-defect fix, say which item should have caught it, why it did not,
and propose the amendment
