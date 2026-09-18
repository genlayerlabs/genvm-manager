# TypeScript Tests

```bash
genvm-tool test run --filter-tag typescript
```

Collected from every tracked `package.json` declaring a `test` script, one case
per `*.test.ts` under it, plus `<project>/npm-ci` and `<project>/typecheck`.
Today that is only `webdriver/src/prj`

Every case except the install `depends_on` it, so `node_modules` is populated
once per project. A dependency is pulled in even when the filter excludes it,
and if it fails the dependents are skipped rather than failing on their own

`needs-web` is on all of them: `npm ci` reaches the registry

## Running One Directly

The project's `test` script forwards a file, so what the runner does is:

```bash
cd webdriver/src/prj
npm ci
npm test -- src/render.test.ts   # omit the file to run all of them
npm run typecheck
```

## Extra Tags

Same marker as Rust ([rust.md](rust.md)), parsed by the shared
`tests/runner/genvm_tool_plugins/source_tags.py`:

```ts
// genvm-tool-test-tags: feature-web-render
```

They add to `typescript` plus `unit` and `needs-web`. Unknown tags fail
collection
