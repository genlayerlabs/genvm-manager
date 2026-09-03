---
name: build
description: Builds the GenVM project. Use after making code changes to compile Rust binaries.
---

Build procedure: `docs/contributing/howto/building/build.md` (debug build;
read it first). Related: `building/runners.md`, `releasing/release-build.md`,
`extending/modify-runner.md` under the same howto root.

Claude-specific:

- Build binaries with
  `bash .agents/skills/build/scripts/run-ninja.sh -C build all/bin` instead of
  raw ninja — it is silent on success and prints output only on failure, which
  saves tokens.
- See also: `/submodules` (multi-repo commits, `?submodules=1`), `/test`,
  `/macos` (never build runners natively on macOS).
