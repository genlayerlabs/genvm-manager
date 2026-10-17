# Explanation

Why the repository is shaped the way it is. Nothing here is normative — a rule
lives in the spec, and these pages only motivate it and link out

- [docs-layout.md](docs-layout.md) — the 4 kinds of page and where each belongs
- [executor-lines.md](executor-lines.md) — why several executor lines ship side by side, and what it costs
- [fuzz.md](fuzz.md) — why fuzz targets get fake entropy and no CmpLog
- [merge-model.md](merge-model.md) — why manager branch tips own executor refs
- [shared-submodule-cache.md](shared-submodule-cache.md) — why submodules are worktrees of one cache repo, not clones
- [vendored-trees.md](vendored-trees.md) — why third-party sources are patch series, not forks
