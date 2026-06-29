"""Wiki_Medicina taxonomy public API.

The canonical taxonomy policy is defined in ``wiki.taxonomy.policy``; schema
constants are compatibility projections derived from that single source.
"""
from __future__ import annotations

from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.audit import (
    _canonical_directory_paths,
    taxonomy_audit,
    taxonomy_tree,
)
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.migration import (
    _default_migration_receipt_path,
    _join_wiki_relative_dir,
    _load_json_file,
    _missing_parent_dirs,
    _plan_wiki_dir,
    _write_json_atomic,
    apply_taxonomy_migration,
    rollback_taxonomy_migration,
    taxonomy_migration_plan,
)
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.normalize import (
    _DRIVE_RE,
    _UNSAFE_TAXONOMY_RE,
    _UNSAFE_TITLE_RE,
    _fold_taxonomy_segment,
    _safe_relative_dir,
    normalize_taxonomy,
    safe_title,
)
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.policy import (
    CANONICAL_TAXONOMY_POLICY,
    TaxonomyAreaPolicy,
    TaxonomySpecialtyPolicy,
)
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.resolve import (
    _NEAR_DUPLICATE_CUTOFF,
    TaxonomyDecisionRequired,
    _canonicalize_taxonomy_parts,
    _format_suggestions,
    _match_existing_segment,
    _suggest_existing_segments,
    _validate_taxonomy_not_title,
    _visible_child_dirs,
    resolve_target_for_note,
    resolve_taxonomy,
    target_for_note,
)
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.schema import (
    CANONICAL_AREA_ALIASES,
    CANONICAL_TAXONOMY,
    CANONICAL_TAXONOMY_ALIASES,
    TaxonomyResolution,
    _canonical_area_aliases_by_fold,
    _canonical_roots_by_fold,
    _canonical_specialties_by_fold,
    _canonical_specialties_for_root,
    canonical_taxonomy_invariants,
    canonical_taxonomy_tree,
)
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.status import (
    TAXONOMY_STATUS_SCHEMA,
    render_taxonomy_status_markdown,
    taxonomy_status,
)

__all__ = [
    "CANONICAL_TAXONOMY",
    "CANONICAL_TAXONOMY_POLICY",
    "CANONICAL_AREA_ALIASES",
    "CANONICAL_TAXONOMY_ALIASES",
    "TaxonomyAreaPolicy",
    "TaxonomyResolution",
    "TaxonomySpecialtyPolicy",
    "TaxonomyDecisionRequired",
    "TAXONOMY_STATUS_SCHEMA",
    "_DRIVE_RE",
    "_NEAR_DUPLICATE_CUTOFF",
    "_UNSAFE_TAXONOMY_RE",
    "_UNSAFE_TITLE_RE",
    "_canonical_directory_paths",
    "_canonical_area_aliases_by_fold",
    "_canonical_roots_by_fold",
    "_canonical_specialties_by_fold",
    "_canonical_specialties_for_root",
    "_canonicalize_taxonomy_parts",
    "_default_migration_receipt_path",
    "_fold_taxonomy_segment",
    "_format_suggestions",
    "_join_wiki_relative_dir",
    "_load_json_file",
    "_match_existing_segment",
    "_missing_parent_dirs",
    "_plan_wiki_dir",
    "_safe_relative_dir",
    "_suggest_existing_segments",
    "_validate_taxonomy_not_title",
    "_visible_child_dirs",
    "_write_json_atomic",
    "apply_taxonomy_migration",
    "canonical_taxonomy_tree",
    "canonical_taxonomy_invariants",
    "normalize_taxonomy",
    "resolve_target_for_note",
    "resolve_taxonomy",
    "render_taxonomy_status_markdown",
    "rollback_taxonomy_migration",
    "safe_title",
    "target_for_note",
    "taxonomy_audit",
    "taxonomy_migration_plan",
    "taxonomy_status",
    "taxonomy_tree",
]
