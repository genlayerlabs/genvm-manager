# genvm-tool

Lives at `support/tools/genvm-tool` (on `PATH` as `genvm-tool` in the dev shell;
outside it, call `support/tools/genvm-tool/genvm-tool`).

## Command reference

The full man page is generated from the parser itself:

```bash
genvm-tool --print-manpage | man -l -
```

## genvm-tool test

```sh
genvm-tool test run [--filter-name REGEX] [--filter-tag EXPR] [--filter-continue FILE] [--fail-fast]
```

All given filters must match (they are AND-ed). `--filter-name` is an
unanchored regex over the test name; `--filter-tag` is a boolean tag
expression like `(a|b)&!c`.

To list every test a filter selects (several hundred entries — pipe it, or use
the json log format for machine consumption):

```sh
genvm-tool --log-format=json test show test
```

Full testing workflow: [testing/README.md](testing/README.md).

## genvm-tool git

Multi-repo helpers over the manager and every executor submodule:

| Command | Effect |
|---|---|
| `genvm-tool git ls` | list tracked files across all repos |
| `genvm-tool git list-repo` | list the manager and every executor submodule repo |
| `genvm-tool git create-branches` | create a feature branch across the sub-repos that have new content |
| `genvm-tool git check-for-push` | read-only pre-push status; prints the suggested push command |

Where they fit in the workflow:
[first-contribution tutorial](../tutorial/first-contribution.md),
[submodules.md](committing/submodules.md).
