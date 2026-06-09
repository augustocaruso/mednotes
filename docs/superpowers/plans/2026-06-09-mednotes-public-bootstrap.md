# MedNotes Public Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bootstrap the public `mednotes` repository shape from the architecture proposal.

**Architecture:** Keep reusable educational content and shared hook logic in `core/`. Keep host-specific packaging in `adapters/antigravity/` and `adapters/opencode/`, with tests proving the Antigravity bundle is built from `core/` instead of copied by hand.

**Tech Stack:** Python 3.9 standard library, `uv` for Python workflow, TypeScript source for the opencode adapter, GitHub-ready docs.

---

### Task 1: Repository Contract Tests

**Files:**
- Create: `tests/test_public_scaffold.py`

- [ ] **Step 1: Write failing tests**

```bash
python3 -m unittest tests/test_public_scaffold.py -v
```

Expected: FAIL because `core/scripts/public_guard.py` and `adapters/antigravity/build.py` do not exist yet.

- [ ] **Step 2: Implement the minimal scaffold**

Create `core/skills/`, `core/agents/`, `core/scripts/`, `adapters/antigravity/`, and `adapters/opencode/`.

- [ ] **Step 3: Run tests**

```bash
python3 -m unittest tests/test_public_scaffold.py -v
```

Expected: PASS.

### Task 2: Public GitHub Surface

**Files:**
- Create: `README.md`
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `LICENSE`

- [ ] **Step 1: Document the public mental model**

Write a README that explains `core/` versus `adapters/`, current status, and why Gemini CLI is discontinued for this project.

- [ ] **Step 2: Add local verification command**

Expose `uv run python -m unittest` through `pyproject.toml` docs and keep generated bundles out of Git with `.gitignore`.

- [ ] **Step 3: Verify status**

```bash
git status --short
python3 -m unittest discover -s tests -v
```

Expected: tracked source files are ready, generated `dist/` is ignored, and tests pass.
