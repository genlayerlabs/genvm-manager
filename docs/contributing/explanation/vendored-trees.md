# Why Vendored Trees Are Patches, Not Forks

Some third-party sources, wasmtime above all, need changes no upstream release
exposes. What is tracked is a pinned upstream commit plus numbered
`git format-patch` files; the tree itself is git-ignored and materialized on
demand ([git-third-party.md](../howto/committing/git-third-party.md))

A fork would hide our delta in a second history and drift silently; as a patch
series, the delta *is* the tracked file, it shows up in an ordinary pull request
diff, and an upstream bump fails loudly instead of quietly diverging

Committing the modified source instead would bury the same delta in a few
hundred thousand lines, where a change to upstream code and a change to ours
look identical

## What It Costs

A checkout is not buildable until the trees are materialized, and edits live in
a nested repository whose history must be turned back into patches to survive.
Nix reads the pinned commit and patches directly, so an unsaved edit is
invisible to it and a build can silently use the previous state
