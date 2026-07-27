#!/usr/bin/env python3

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / ".github/workflows/release.yaml"
USES_PATTERN = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)
SHA_REF_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def fail(message: str) -> None:
	raise SystemExit(f"release workflow policy: {message}")


def reachable_workflows(entry: Path) -> list[Path]:
	pending = [entry]
	visited: set[Path] = set()

	while pending:
		workflow = pending.pop()
		if workflow in visited:
			continue
		if not workflow.is_file():
			fail(f"referenced workflow does not exist: {workflow.relative_to(ROOT)}")
		visited.add(workflow)

		for uses in USES_PATTERN.findall(workflow.read_text()):
			if uses.startswith("./.github/workflows/"):
				pending.append(ROOT / uses.removeprefix("./"))

	return sorted(visited)


def main() -> None:
	workflows = reachable_workflows(ENTRY)
	for workflow in workflows:
		for uses in USES_PATTERN.findall(workflow.read_text()):
			if uses.startswith("./"):
				continue
			if "@" not in uses:
				fail(
					f"{workflow.relative_to(ROOT)} has an external action without a ref: {uses}"
				)
			_, ref = uses.rsplit("@", 1)
			if not SHA_REF_PATTERN.fullmatch(ref):
				fail(
					f"{workflow.relative_to(ROOT)} uses mutable external action ref: {uses}"
				)

	release = ENTRY.read_text()
	for forbidden in ('git push origin "$TAG"', 'git tag "$TAG"'):
		if forbidden in release:
			fail(f"release publication creates the tag before the draft is complete: {forbidden}")

	required = (
		"target_commitish: ${{ github.sha }}",
		"draft: true",
		"overwrite_files: true",
		'gh release edit "$TAG" --draft=false',
		"actions/attest@59d89421af93a897026c735860bf21b6eb4f7b26",
	)
	for fragment in required:
		if fragment not in release:
			fail(f"release workflow is missing required immutable-publication control: {fragment}")

	print(
		f"release workflow policy passed for {len(workflows)} reachable workflow file(s)"
	)


if __name__ == "__main__":
	main()
