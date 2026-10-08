---
name: build
description: Builds the GenVM project. Use after making code changes to compile Rust binaries.
---

Build procedure: `docs/contributing/howto/building/build.md` (debug build;
read it first). Related: `building/runners.md`, `releasing/release-build.md`,
`extending/modify-runner.md` under the same howto root.

Claude-specific:

- Build binaries with
  `bash -o pipefail -c 'ninja -C build all/bin 2>&1 | tee "$TMPDIR/ninja.log" | tail -n 40'`;
  on failure grep the log for `error` instead of rebuilding to see it again
- See also: `/submodules` (multi-repo commits, `?submodules=1`), `/test`,
  `/macos` (never build runners natively on macOS).
