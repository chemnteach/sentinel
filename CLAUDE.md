# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Sentinel is a multi-agent code governance system (v2.0.0) that routes model inference through a pluggable public LLM provider (OpenAI, Anthropic, GitHub Models, or GitHub Copilot CLI). It performs automated codebase assessment and governed remediation across three phases with human gates at key decision points.

## Build & Test Commands

```bash
# Install (with dev dependencies)
pip install -e ".[dev]"

# Run all tests (pytest with async auto-mode, coverage enabled)
pytest

# Run a single test file
pytest tests/test_agents.py

# Run a specific test
pytest tests/test_state_machine.py -k "test_tdd_gate"

# Run with verbose output
pytest -v
```

Tests use `respx` for HTTP mocking and `pytest-asyncio` with `asyncio_mode = "auto"`. Coverage reports are generated automatically (`--cov=sentinel --cov-report=term-missing`).

## CLI Entry Point

```bash
sentinel assess <repo_path>          # Phase 1: codebase assessment
sentinel assess --continue           # Resume after tech debt triage
sentinel triage --list               # View tech debt items
sentinel triage --approve <id>       # Triage a tech debt item
sentinel plan --show / --approve     # View/approve remediation plan
sentinel govern --start              # Phase 2: governed remediation
sentinel task --list / --merged <id> # Manage tasks
sentinel dispute --resolve <id> --winner architect|coder
sentinel fer --submit <json>         # Phase 3: feature enhancement requests
sentinel feasibility --run <fer_id>  # Run feasibility assessment
sentinel spec --run <fer_id>         # Generate enhancement spec
sentinel defect --submit <json>      # Ingest bug report / stack trace
sentinel defect --list / --show <id> # View defect status & details
sentinel defect --triage <id>        # Triage defect (--approve/--defer/--reject)
sentinel rca --run <id> / --show <id># Run 3-tier RCA & generate advisory brief
sentinel status                      # Overall system status
```

Entry point: `sentinel.cli:main` (Click group).

## Architecture

### Three-Phase Workflow

**Phase 1 — Assessment**:
1. Repository Discovery (Layer 0a Cartographer), Dependency Mapping (Layer 0b), Architectural Checklist (Layer 0d)
2. Module-by-Module Review (Layers 1–4 skill cascade), dual-lane: deterministic analyzers + AI reasoning, reconciled
3. Cross-Module Synthesis → **semantic grouping** (Stage 4b, structural lens) + **concept themes** (Stage 4c, conceptual/embedding lens)
4. Remediation pattern proposal per group (Layer 5b)
5. Quality Scorecard gate (strict mode: coverage certificate + confidence/completeness gates) → **HUMAN GATE**
6. Tech Debt Triage → **HUMAN GATE**
7. Remediation Planning (Layer 5) → **HUMAN GATE**

**Phase 2 — Governed Remediation**: Drives each task through the full lifecycle state machine (PLANNED → COMPLETE) with architect review, QA verification, documentation gates, and PR creation.

**Phase 3 — Feature Enhancement Requests (FERs)**: Submit FERs → feasibility assessment (Layer 7a) → spec writing (Layer 7b) → backwards compatibility check (Layer 7c) → task generation → Phase 2 execution.

### Key Design Principles

- **Orchestrator is pure Python** (`orchestrator/main.py`) — no AI component. It manages state transitions, assembles context for agents, and is the sole writer of JSON state files.
- **Agents are stateless** between API calls. Context is reconstructed from `sentinel/state/` JSON files before every invocation.
- **Agents never self-approve** and never self-select skills. The Orchestrator decides which skill to inject.
- **Human gates** interrupt execution via `HumanGateRequired` exceptions. The CLI catches these and displays instructions.
- **TDD gate is non-bypassable**: Implementation cannot begin until both the implementation plan AND test stubs are separately approved by the Architect.

### Agent System

Four agents, all inheriting from `BaseAgent` (`agents/base.py`):
- **Architect** — reviews, plans, prioritizes, approves TODOs
- **Coder** — submits TODOs, implements, self-reviews
- **QA** — verifies fixes against original findings
- **Docs** — updates affected documentation (mandatory gate)

Each agent's `run()` method composes: agent identity header + skill file body = system prompt, then calls the configured model provider.

### Layered Skill Framework

Skills are Markdown files in `sentinel/skills/skill_files/` with YAML front-matter. The `SkillLoader` strips front-matter and injects the body as the agent's system prompt. Skill names are constants in `skills/loader.py::Skills`.

| Layer | Purpose | Skills |
|-------|---------|--------|
| 0 | Discovery | Cartographer, Dependency Analyzer, Scope Guardian, Architectural Checklist |
| 1 | Planning | Review Prioritizer |
| 2 | Core Review | Quick Review (mandatory), Correctness, Security, Data Integrity, API Contract Compatibility, Data-Flow/Trust-Boundary, Config Drift, Build/Release Integrity, Change Impact, Operational Risk |
| 3 | Specialist | Performance, Maintainability, Testability, Error Handling, Concurrency |
| 4 | Structural | Code Smell Detector, Refactoring Advisor |
| 5 | Fix Planning | Fix Planner, Remediation Pattern Proposer |
| 6 | Verification | Verification Reviewer, Cross-Repo Verification, Transition Manifest Generator |
| 7 | Feature Enhancement | Feasibility Analyzer, Spec Writer, Backwards Compat Checker |
| 8 | Defect RCA & Remediation | Root Cause Analyzer, Bug Remediation Spec Writer |

(32 skill files total in `sentinel/skills/skill_files/`.) The reviewer (`assess/reviewer.py`) runs Layer 2a unconditionally on every module, then selects specialists per `SENTINEL_REVIEW_PROFILE`: `reliability-first` (default) keeps a fixed reliability subset always-on (correctness, security, data-integrity, error-handling) + concurrency when triggered; `triggered` lets Quick Review findings drive the full specialist suite via keyword matching.

### State Machine

`orchestrator/state_machine.py` defines the full task lifecycle (`TaskState`, 18 states). Two hard governance gates:
- **TDD Gate** (TODO_APPROVED → IN_PROGRESS): requires both `todo_approved` and `test_stubs_approved`
- **Documentation Gate** (QA_PASS → DOC_UPDATED): requires `doc_updated = True`

### State Persistence

All state lives in JSON files under `sentinel/state/` (configurable via `SENTINEL_STATE_DIR`). The `StateStore` commits state to git after each significant step (when the state dir is inside a repo). State files include: `repositories.json`, `modules.json`, `dependencies.json`, `findings.json`, `semantic_groups.json`, `concept_themes.json`, `remediation_patterns.json`, `coverage_certificate.json`, `coverage_report.json`, `quality_scorecard.json`, `reconciliation_report.json`, `tech_debt.json`, `plan.json`, `tasks.json`, `reviews.json`, `disputes.json`, `audit_log.json`, `fers.json`, `feasibility.json`, `specs.json`, `pr_manifests.json`, `defects.json`, `rca_reports.json`, `defect_specs.json`.

### API Client

`api/client.py` provides the provider clients (`ModelClient` for OpenAI/GitHub Models, `AnthropicClient`, `CopilotCliClient`, `NoOpModelClient`) behind `create_model_client()`. They retry with backoff on rate limits and server errors, record per-call telemetry to `llm_telemetry.json`, and strip markdown fences from JSON responses.

## Configuration

All settings via environment variables (see `.env.example`). Key vars:
- `SENTINEL_PROVIDER` — `openai` | `anthropic` | `github-models` | `copilot-cli` (auto-detected from keys when unset)
- `SENTINEL_MODEL` — default `gpt-5.1`
- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GITHUB_TOKEN` + `SENTINEL_ALLOW_GITHUB_MODELS=true` — provider credentials
- `SENTINEL_REPO_PATH` — target repo (default: `.`); `SENTINEL_STATE_DIR` — state output dir
- `SENTINEL_STRICT_QUALITY_MODE` — full-coverage review (no chunk cap/sampling) + coverage & quality-scorecard gates
- `SENTINEL_REVIEW_PROFILE` — `triggered` | `reliability-first` (default)
- `SENTINEL_ENABLE_CONCEPT_THEMES` — Stage 4c conceptual-lens grouping (default `true`); set `false` to skip the embedding+HDBSCAN clustering (alternate lens, not a coverage gate — not forced on by strict mode)
- `SENTINEL_CHUNK_MAX_LINES` / `SENTINEL_CHUNK_OVERLAP_LINES` — code chunking (default `300` / `80`)
- `SENTINEL_LOCAL_ONLY_MODE` — disable all external model calls (dry-run/offline)

Config is a singleton: call `load_config()` once, then `get_config()` everywhere.

## Code Conventions

- Python 3.11+, type hints throughout (`from __future__ import annotations`)
- Pydantic for validation, Click for CLI, Rich for terminal output
- `httpx` (not `requests`) for HTTP
- All agent calls return `ModelResponse` or parsed JSON dicts
- Per-task lifecycle is driven inline by `Orchestrator._run_task` (`sentinel/orchestrator/main.py`); workflow sequencing lives there alongside agent calls and state mutations
