"""Single source of truth for the Wiki_Medicina taxonomy policy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TAXONOMY_POLICY_VERSION = "2026-05-15.taxonomy-v1"

TaxonomyAliasKind = Literal["area", "specialty"]


@dataclass(frozen=True)
class TaxonomyAliasPolicy:
    alias: str
    canonical_target: tuple[str, ...]
    kind: TaxonomyAliasKind
    reason: str
    migration_safe: bool = True
    requires_human_review: bool = False


@dataclass(frozen=True)
class TaxonomySpecialtyPolicy:
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaxonomyAreaPolicy:
    name: str
    aliases: tuple[str, ...] = ()
    specialties: tuple[TaxonomySpecialtyPolicy, ...] = ()


CANONICAL_TAXONOMY_POLICY: tuple[TaxonomyAreaPolicy, ...] = (
    TaxonomyAreaPolicy(
        name="1. Clínica Médica",
        aliases=("Clinica Medica", "Clínica Médica"),
        specialties=(
            TaxonomySpecialtyPolicy("Cardiologia"),
            TaxonomySpecialtyPolicy("Dermatologia"),
            TaxonomySpecialtyPolicy("Endocrinologia"),
            TaxonomySpecialtyPolicy("Gastroenterologia"),
            TaxonomySpecialtyPolicy("Geriatria"),
            TaxonomySpecialtyPolicy("Hematologia"),
            TaxonomySpecialtyPolicy("Imunologia"),
            TaxonomySpecialtyPolicy("Infectologia"),
            TaxonomySpecialtyPolicy("Medicina Interna", aliases=("Medicina Interna",)),
            TaxonomySpecialtyPolicy("Nefrologia"),
            TaxonomySpecialtyPolicy("Neurologia"),
            TaxonomySpecialtyPolicy("Nutrologia"),
            TaxonomySpecialtyPolicy("Oncologia"),
            TaxonomySpecialtyPolicy("Pneumologia"),
            TaxonomySpecialtyPolicy("Reumatologia"),
            TaxonomySpecialtyPolicy("Semiologia"),
            TaxonomySpecialtyPolicy("Psiquiatria"),
        ),
    ),
    TaxonomyAreaPolicy(
        name="2. Cirurgia",
        aliases=("Cirurgia",),
        specialties=(
            TaxonomySpecialtyPolicy("Cirurgia Geral", aliases=("Cirurgia_Geral", "Cirurgia Geral")),
            TaxonomySpecialtyPolicy("Clínica Cirúrgica", aliases=("Clinica Cirurgica", "Clínica Cirúrgica")),
            TaxonomySpecialtyPolicy("Oftalmologia"),
            TaxonomySpecialtyPolicy("Urologia"),
            TaxonomySpecialtyPolicy("Trauma"),
            TaxonomySpecialtyPolicy("Anestesiologia"),
        ),
    ),
    TaxonomyAreaPolicy(
        name="3. Ginecologia e Obstetrícia",
        aliases=(
            "#. Ginecologia e Obstetricia",
            "3. Ginecologia e Obstetricia",
            "Ginecologia_Obstetricia",
            "Ginecologia e Obstetricia",
            "Ginecologia e Obstetrícia",
        ),
        specialties=(
            TaxonomySpecialtyPolicy("Ginecologia", aliases=("Ginecologia",)),
            TaxonomySpecialtyPolicy("Obstetrícia", aliases=("Obstetricia",)),
        ),
    ),
    TaxonomyAreaPolicy(
        name="4. Pediatria",
        aliases=("Pediatria",),
        specialties=(
            TaxonomySpecialtyPolicy("Pediatria"),
            TaxonomySpecialtyPolicy("Neonatologia"),
            TaxonomySpecialtyPolicy("Puericultura"),
            TaxonomySpecialtyPolicy(
                "Infecto Pediátrica",
                aliases=("Infecto Pediatrica", "Infecto Pediátrica", "Infectopediatria"),
            ),
        ),
    ),
    TaxonomyAreaPolicy(
        name="5. Medicina Preventiva",
        aliases=("Medicina Preventiva",),
        specialties=(
            TaxonomySpecialtyPolicy("Medicina Preventiva"),
            TaxonomySpecialtyPolicy("SUS"),
            TaxonomySpecialtyPolicy("Epidemiologia"),
            TaxonomySpecialtyPolicy("Ética Médica", aliases=("Etica Medica", "Ética Médica")),
            TaxonomySpecialtyPolicy("Saúde do Trabalho", aliases=("Saude do Trabalho", "Saúde do Trabalho")),
        ),
    ),
)


def iter_taxonomy_aliases() -> tuple[TaxonomyAliasPolicy, ...]:
    aliases: list[TaxonomyAliasPolicy] = []
    for area in CANONICAL_TAXONOMY_POLICY:
        aliases.extend(
            TaxonomyAliasPolicy(
                alias=alias,
                canonical_target=(area.name,),
                kind="area",
                reason="legacy_no_accent" if "Obstetricia" in alias else "legacy_short_name",
            )
            for alias in area.aliases
        )
        for specialty in area.specialties:
            aliases.extend(
                TaxonomyAliasPolicy(
                    alias=alias,
                    canonical_target=(area.name, specialty.name),
                    kind="specialty",
                    reason="legacy_no_accent" if "Obstetricia" in alias else "legacy_specialty_alias",
                )
                for alias in specialty.aliases
                if alias != specialty.name
            )
    return tuple(aliases)
