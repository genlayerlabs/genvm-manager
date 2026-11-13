"""Tag registry: expansion of hierarchical feature tags and validation."""

import json
from dataclasses import dataclass
from pathlib import Path

FEATURE_PREFIX = 'feature-'


@dataclass(frozen=True)
class Registry:
	general: dict[str, str]
	features: dict[str, str]

	@property
	def known(self) -> frozenset[str]:
		return frozenset(self.general) | frozenset(
			FEATURE_PREFIX + name for name in self.features
		)


def load(path: Path) -> Registry:
	try:
		raw = json.loads(path.read_text())
	except json.JSONDecodeError as error:
		raise ValueError(f'{path}: {error}') from error
	if not isinstance(raw, dict):
		raise ValueError(f'{path}: tag registry must be an object')

	values: dict[str, dict[str, str]] = {}
	for section in ('general', 'features'):
		value = raw.get(section)
		if not isinstance(value, dict) or not all(
			isinstance(name, str) and isinstance(description, str)
			for name, description in value.items()
		):
			raise ValueError(f'{path}: {section} must map tag names to descriptions')
		values[section] = value

	return Registry(general=values['general'], features=values['features'])


def expand(tags: frozenset[str], *, registry: Registry | None) -> frozenset[str]:
	"""
	Add every declared ancestor of a hierarchical feature tag.

	``feature-web-request-body`` also emits ``feature-web-request`` and
	``feature-web``, so selecting a root selects the whole subtree while matching
	stays exact. Ancestors are declared dash-prefixes in the registry, preserving
	compound path segments such as ``runner-map-vm``. Without a registry, tags are
	returned unchanged.
	"""
	expanded = set(tags)
	if registry is None:
		return frozenset(expanded)
	for tag in tags:
		if not tag.startswith(FEATURE_PREFIX):
			continue
		expanded.update(
			FEATURE_PREFIX + name
			for name in registry.features
			if tag.startswith(FEATURE_PREFIX + name + '-')
		)
	return frozenset(expanded)


def unknown(registry: Registry, tags: frozenset[str]) -> frozenset[str]:
	return tags - registry.known
