"""Canonical Wiki_Medicina taxonomy schema.

The executable taxonomy policy lives in :mod:`wiki.taxonomy.policy`.  This
module exposes the historical constants/helpers as derived compatibility APIs.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.normalize import _fold_taxonomy_segment, safe_title
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.policy import (
    CANONICAL_TAXONOMY_POLICY,
    TAXONOMY_POLICY_VERSION,
    TaxonomyAreaPolicy,
    iter_taxonomy_aliases,
)
from mednotes.kernel.base import JsonObject


def _area_by_name(name: str) -> TaxonomyAreaPolicy:
    for area in CANONICAL_TAXONOMY_POLICY:
        if area.name == name:
            return area
    raise KeyError(name)


GINECOLOGY_OBSTETRICS_AREA = "3. Ginecologia e Obstetrícia"
_GINECOLOGY_OBSTETRICS_POLICY = _area_by_name(GINECOLOGY_OBSTETRICS_AREA)
GINECOLOGY_OBSTETRICS_CHILDREN = tuple(specialty.name for specialty in _GINECOLOGY_OBSTETRICS_POLICY.specialties)
GINECOLOGY_OBSTETRICS_LEGACY_ALIASES = (
    *_GINECOLOGY_OBSTETRICS_POLICY.aliases,
    *(
        alias
        for specialty in _GINECOLOGY_OBSTETRICS_POLICY.specialties
        for alias in specialty.aliases
        if alias != specialty.name
    ),
)


def canonical_taxonomy_invariants() -> dict[str, Any]:
    alias_mappings = [
        {
            "alias": alias.alias,
            "canonical": "/".join(alias.canonical_target),
            "kind": alias.kind,
            "reason": alias.reason,
            "migration_safe": alias.migration_safe,
            "requires_human_review": alias.requires_human_review,
        }
        for alias in iter_taxonomy_aliases()
    ]
    return {
        "taxonomy_policy_version": TAXONOMY_POLICY_VERSION,
        "areas": [area.name for area in CANONICAL_TAXONOMY_POLICY],
        "specialty_paths": [
            f"{area.name}/{specialty.name}"
            for area in CANONICAL_TAXONOMY_POLICY
            for specialty in area.specialties
        ],
        "legacy_alias_mappings": alias_mappings,
        "ginecologia_obstetricia": {
            "area": GINECOLOGY_OBSTETRICS_AREA,
            "children": list(GINECOLOGY_OBSTETRICS_CHILDREN),
            "legacy_aliases": list(GINECOLOGY_OBSTETRICS_LEGACY_ALIASES),
        }
    }


CANONICAL_TAXONOMY: tuple[tuple[str, tuple[str, ...]], ...] = (
    *(
        (area.name, tuple(specialty.name for specialty in area.specialties))
        for area in CANONICAL_TAXONOMY_POLICY
    ),
)


CANONICAL_AREA_ALIASES: tuple[tuple[str, str], ...] = (
    *((alias, area.name) for area in CANONICAL_TAXONOMY_POLICY for alias in area.aliases),
)


CANONICAL_TAXONOMY_ALIASES: tuple[tuple[str, str, str], ...] = (
    *(
        (alias, area.name, specialty.name)
        for area in CANONICAL_TAXONOMY_POLICY
        for specialty in area.specialties
        for alias in specialty.aliases
    ),
)


@dataclass(frozen=True)
class TaxonomyResolution:
    requested_taxonomy: str
    taxonomy: str
    parts: tuple[str, ...]
    canonicalized: tuple[dict[str, str], ...]
    new_dirs: tuple[str, ...]

    @property
    def has_new_dirs(self) -> bool:
        return bool(self.new_dirs)

    def to_json(self, wiki_dir: Path, title: str | None = None) -> JsonObject:
        data: JsonObject = {
            "wiki_dir": str(wiki_dir),
            "requested_taxonomy": self.requested_taxonomy,
            "taxonomy": self.taxonomy,
            "parts": list(self.parts),
            "canonicalized": list(self.canonicalized),
            "new_dirs": list(self.new_dirs),
            "requires_new_folder": self.has_new_dirs,
        }
        if title is not None:
            data["title"] = title
            data["target_path"] = str(wiki_dir.joinpath(*self.parts, f"{safe_title(title)}.md"))
        return data

def _canonical_roots_by_fold() -> dict[str, str]:
    return {_fold_taxonomy_segment(root): root for root, _specialties in CANONICAL_TAXONOMY}


def _canonical_area_aliases_by_fold() -> dict[str, str]:
    mapping = _canonical_roots_by_fold()
    for alias, root in CANONICAL_AREA_ALIASES:
        mapping[_fold_taxonomy_segment(alias)] = root
    return mapping


def _canonical_specialties_by_fold() -> dict[str, tuple[str, str]]:
    mapping: dict[str, tuple[str, str]] = {}
    for root, specialties in CANONICAL_TAXONOMY:
        for specialty in specialties:
            mapping[_fold_taxonomy_segment(specialty)] = (root, specialty)
            mapping[_fold_taxonomy_segment(specialty.replace(" ", "_"))] = (root, specialty)
    for alias, root, specialty in CANONICAL_TAXONOMY_ALIASES:
        mapping[_fold_taxonomy_segment(alias)] = (root, specialty)
    return mapping


def _canonical_specialties_for_root(root: str) -> dict[str, str]:
    specialties = next((items for candidate, items in CANONICAL_TAXONOMY if candidate == root), ())
    mapping = {_fold_taxonomy_segment(specialty): specialty for specialty in specialties}
    for alias, alias_root, specialty in CANONICAL_TAXONOMY_ALIASES:
        if alias_root == root:
            mapping[_fold_taxonomy_segment(alias)] = specialty
    return mapping


def canonical_taxonomy_tree() -> dict[str, Any]:
    areas = []
    for root, specialties in CANONICAL_TAXONOMY:
        areas.append({"area": root, "specialties": list(specialties)})
    return {"schema": "medical-notes-workbench.canonical-taxonomy.v1", "areas": areas}
