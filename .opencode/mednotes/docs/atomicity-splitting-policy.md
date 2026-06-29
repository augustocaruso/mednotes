# Atomicity Splitting Policy

This is the canonical human-readable policy for deciding whether a Wiki note
must be split into smaller notes. The executable gate lives in the vocabulary
DB apply path (`wiki.vocabulary_ingestion`); prompts, skills, agents, docs and
JSON contracts must reference this policy instead of restating their own
thresholds.

## Contract

- Atomicity means `1 meaning canônico = 1 nota Wiki`.
- The vocabulary DB decides whether a split is applicable.
- Meaning comes from the note body, not from the file name or title.
- A title-only signal never authorizes a split.
- A long note is a review signal, not sufficient evidence by itself.
- A short proposed child note is a fragmentation risk, not a reason to force a
  split.

Every `deferred_work_items[].reason=non_atomic_note` item must include a
body-based `semantic_signal`. Missing or weak body evidence blocks as
`semantic_ingestion.atomicity_signal_required` and requires parent/human review.

## semantic_signal

`semantic_signal` must describe why the note body contains more than one
developed canonical concept. It must include:

- `score`: explicit semantic score when the curator can estimate one.
- `evidence[]`: evidence codes from the table below.
- `concepts[]`: at least two developed concepts found in the note body.
- `relationship_score`: probability that the note is a valid relationship note
  instead of a non-atomic note.
- `fragment_risk`: `high` when the split would create underdeveloped children.
- `child_note_estimates[]`: estimated body size for each proposed child note
  when a split is being considered.

Evidence weights used by the vocabulary DB:

| evidence code | weight |
| --- | ---: |
| `multiple_canonical_entities` | 0.30 |
| `different_entity_types` | 0.25 |
| `independent_definition_blocks` | 0.20 |
| `independent_management_blocks` | 0.20 |
| `independent_pathophysiology_blocks` | 0.15 |
| `separable_sections` | 0.15 |
| `linker_ambiguity` | 0.15 |

The DB computes `semantic_score` as the maximum of the explicit `score` and the
weighted evidence score, capped at 1.0. If `concepts[]` has two or more items,
`multiple_canonical_entities` is added. If those concepts have two or more
entity types, `different_entity_types` is added.

## Size Gate

Size is a brake and review priority, not the split motor.

- Current note `> mean + 1 standard deviation`: enters the review-priority
  path, but still needs semantic body evidence.
- Current note `<= mean + 1 standard deviation`: only advances with strong
  semantic evidence, such as multiple canonical entities, different entity
  types, independent body blocks, or real linker ambiguity.
- Proposed child note below `240` body characters, or below 25% of the mean body
  size when that is larger, is treated as fragmentation risk.

The phrase `mean + 1 standard deviation` is the durable threshold contract. The
current implementation stores the calculated threshold in
`body_size_gate.long_note_threshold_chars`.

## Decisions

The vocabulary DB maps `semantic_signal` to one of these decisions:

- `relationship_score >= 0.75` -> `relationship_note_valid`. Keep the note
  whole because the body is mainly about a real relationship, such as disease
  plus drug or diagnosis plus management.
- `semantic_score >= 0.75` and fragmentation risk is not high ->
  `split_required`. This is the only decision that persists as
  `deferred_work_items.status=pending` and can generate an atomicity split plan.
- `semantic_score >= 0.75` with high fragmentation risk ->
  `split_deferred_fragment_risk`. Do not split automatically; require
  parent/human review or keep as a controlled mention inside the main note.
- `semantic_score >= 0.45`, or `semantic_score >= 0.35` when the current note is
  above `mean + 1 standard deviation` -> `split_candidate`. Review candidate
  only; not an automatic split.
- Otherwise -> `no_action`.

All decisions except `split_required` are stored as non-applicable/cancelled for
the DB queue and must not produce an automatic split bundle.

## Workflow Consequences

- The `med-link-graph-curator` collects `semantic_signal`; it does not decide or
  perform the split.
- The vocabulary DB apply path gates the decision and persists only
  `split_required` as pending work.
- `fix-wiki` may create `medical-notes-workbench.atomicity-split-plan.v1` only
  from DB-pending `split_required` work items.
- While an atomicity split plan exists, `fix-wiki` must report a real blocker
  such as `atomicity_split_required`; it must not finish as green health.
- The `med-knowledge-architect` may write only an
  `atomicity-split-bundle.v1` for an official work item, preserving
  `work_id`, `source_path`, `source_hash` and `semantic_signal`.
- `apply-atomicity-split` validates the bundle, mutates Markdown safely, records
  the receipt, marks the DB work item completed and triggers the linker.

## Relationship Notes

A note can be short and still non-atomic, or long and still valid. The deciding
question is whether the note body develops separate canonical concepts that
should each stand as an independent study target. If the body instead explains a
single meaningful relationship between concepts, use `relationship_note_valid`
or `split_candidate`, not `split_required`.
