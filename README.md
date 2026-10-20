# Sentinel

Sentinel is a **multi-agent code governance system** that works with public LLM providers (OpenAI, Anthropic, GitHub Models, GitHub Copilot CLI). It uses **4 specialized AI agents** (Architect, Coder, QA, Docs) and **32 layered review skills** to systematically assess, remediate, and evolve codebases through a structured three-phase process — with human approval gates at every critical decision point.

**The deployment model is simple:** clone Sentinel directly into or alongside the repository you want to analyze, then drive it via the CLI or from within GitHub Copilot Chat in VS Code. No external API keys are required when using the Copilot Chat path.

---

## How It Works: The Three Phases

### Phase 1 — Assessment

Sentinel performs a complete automated audit of your codebase:

1. **Repository Discovery** — the **Cartographer** (Layer 0a — a deterministic local scan, or the Architect agent applying the Cartographer skill) maps all source files, module boundaries, and file types (supports `.cs`, `.py`, `.ts`, `.xml`, `.json`, `.yaml`, and more)
2. **Dependency Mapping** — cross-repo and cross-module dependencies are identified and catalogued
3. **Module-by-Module Layered Review** — each module is passed through the layered review cascade (Layers 1–4), all run by the **Architect agent**:
   - Layer 1 (Review Prioritizer) determines which specialist skills are needed
   - Layer 2a (Quick Review) runs unconditionally on every module
   - Specialist skills (security, correctness, data integrity, API contracts, trust boundaries, config drift, build integrity, change impact, operational risk) run automatically when triggered by findings
   - Deeper layers (performance, maintainability, testability, error handling, concurrency) activate on risk signals
   - Structural layers (code smell detection, refactoring advisor) run last
4. **Cross-Module Synthesis & Dual-Lens Theming** — findings from the deterministic and AI-reasoning lanes are reconciled and deduplicated, then organized two complementary ways: **semantic groups** (structural lens — by where/how the issue occurs in the code) and **concept themes** (conceptual lens — embedding-clustered by problem type). A remediation pattern is proposed per group.
5. **Quality Scorecard** — in strict mode, a coverage certificate plus a confidence scorecard (review completeness, lane agreement, conflict resolution) gate the run; an incomplete or low-confidence assessment pauses for human review before triage.
6. **Tech Debt Triage** — ⛔ **HUMAN GATE**: you review the consolidated register and approve, defer, or exclude each item before anything proceeds.
7. **Remediation Plan Generation** — ⛔ **HUMAN GATE**: Sentinel produces a prioritized fix plan and waits for your explicit approval.

**Phase 1 outputs** (all persisted as auditable JSON in your state directory):

| File | Contents |
|---|---|
| `repositories.json` | Repository manifest |
| `modules.json` | Module inventory with criticality rankings |
| `dependencies.json` | Full dependency graph |
| `findings.json` | All reconciled findings from the skill cascade (deterministic + AI-reasoning lanes) |
| `analysis_pack.json` | Architect cross-cutting assessments (API compatibility, trust boundaries, config drift, build integrity, operational risk) |
| `semantic_groups.json` | Structural-lens grouping of findings (dedup / co-located) |
| `concept_themes.json` | Conceptual-lens themes (embedding-clustered by problem type) |
| `remediation_patterns.json` | A proposed remediation pattern per semantic group |
| `coverage_certificate.json` / `coverage_report.json` | Review coverage — reviewed vs. in-scope lines, per-module gaps |
| `quality_scorecard.json` | Confidence scorecard + gate decisions (review completeness, lane agreement) |
| `tech_debt.json` | Synthesized, deduplicated tech debt register — the triage input |
| `phase1_assessment.json` | Consolidated triage + dependency scope queue |
| `phase1_assessment.md` | Human-readable assessment summary |
| `plan.json` | Remediation plan (produced after triage + `assess --continue`) |
| `audit_log.json` | Append-only event log of all significant actions |

---

### Phase 2 — Governed Remediation

Each approved tech debt item becomes a task that progresses through an **18-state machine** (`TaskState`) with non-bypassable governance gates:

- **TDD gate** (before any implementation begins): both `todo_approved` AND `test_stubs_approved` are required
- **Documentation gate** (before PR creation): `doc_updated` must be confirmed

Happy-path lifecycle (the branch/terminal states `REVIEW_FAIL`, `DISPUTED`, `ESCALATED`, `QA_FAIL`, `BLOCKED`, `CANCELLED` handle failures and exits):

```
PLANNED → ASSIGNED → TODO_SUBMITTED → TODO_APPROVED → IN_PROGRESS
       → SUBMITTED → REVIEW_PASS → QA_PASS → DOC_UPDATED
       → PR_CREATED → HUMAN_MERGED → COMPLETE
```

The Architect agent reviews and approves TODOs. The Coder agent implements. The QA agent verifies fixes against original findings. The Docs agent updates documentation. Every role is separated — **agents never self-approve**.

---

### Phase 3 — Feature Enhancement Requests (FER)

New features enter through a governed intake pipeline:

1. Submit a FER (JSON payload describing the enhancement)
2. Triage decision (approve/defer/reject)
3. Feasibility analysis (Layer 7a skill — technical viability assessment)
4. Feasibility approval ⛔ **HUMAN GATE**
5. Specification writing (Layer 7b skill)
6. Spec approval ⛔ **HUMAN GATE**
7. Backwards compatibility check (Layer 7c skill)
8. Task generation → automatically enters the Phase 2 queue

---

## Model Provider

Sentinel works with public LLM providers, selected with `SENTINEL_PROVIDER` or
auto-detected from the credentials present:

| Provider | `SENTINEL_PROVIDER` | Credentials | Default model |
|---|---|---|---|
| OpenAI | `openai` | `OPENAI_API_KEY` (optional `OPENAI_BASE_URL`) | `gpt-5.1` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` (`pip install -e ".[anthropic]"`) | `claude-sonnet-4-6` |
| GitHub Models | `github-models` | `GITHUB_TOKEN` + `SENTINEL_ALLOW_GITHUB_MODELS=true` | `gpt-4o-mini` |
| GitHub Copilot CLI | `copilot-cli` | a logged-in `copilot` CLI on `PATH` | `gpt-5.1` |

- Override the model with `SENTINEL_MODEL`.
- `SENTINEL_LOCAL_ONLY_MODE=true` disables all external calls for dry-run / offline use.

---

## Installation & Setup

### Prerequisites

- Python 3.11+
- An API key for a supported provider, or the GitHub Copilot CLI (or `SENTINEL_LOCAL_ONLY_MODE=true` for offline use)
- VS Code + GitHub Copilot extension (for the chat-native workflow)

### Step 1 — Clone Sentinel alongside your target repo

The recommended pattern is to clone Sentinel as a sibling to the repository you want to analyze:

```
D:\Git\
  my-target-repo\     ← the repo you want to assess
  sentinel\           ← clone Sentinel here
```

```bash
git clone https://github.com/chemnteach/sentinel.git
cd sentinel
pip install -e ".[dev]"
```

### Step 2 — Configure your environment

```bash
cp .env.example .env
```

Edit `.env` for your project. At minimum set:

```env
# Point to the repo you are analyzing
SENTINEL_REPO_PATH=D:/Git/my-target-repo

# Isolate state per project run (keeps each analysis separate)
SENTINEL_STATE_DIR=state_myproject_run1

# Model provider (or set SENTINEL_PROVIDER=copilot-cli)
OPENAI_API_KEY=sk-...
```

For **offline / dry-run** (no model calls):

```env
SENTINEL_LOCAL_ONLY_MODE=true
```

### Step 3 — Per-project isolation (recommended for teams)

For each new target codebase, create a project-specific repository to hold the state outputs separately from the Sentinel framework source:

```bash
# Add a second remote for your project's state
git remote add project https://github.com/<org>/sentinel-<project>.git
git push project --all
git push project --tags
```

This keeps the Sentinel framework source clean on `origin` while your project state (findings, plans, audit logs) lives on `project`. Push policy:
- **Framework changes** (skills, agents, core logic): push to both `origin` and `project`
- **Project-only artifacts** (state dumps, run outputs): push to `project` only

---

## Running an Assessment

### Handoff / Full-Coverage Run (recommended)

For a complete, reproducible assessment that anyone can run with **one switch**,
use the `full-coverage` profile. It turns on strict mode, **all review skills on
every module**, all quality lanes, alarm classification, and — via the default
`operational` scope — reviews only production/runtime source (not config/data,
tests, scripts, or build files):

```powershell
# Keep the state dir OUTSIDE the target repo and this repo (avoids file-lock /
# git churn); it can be any path.
$env:SENTINEL_STATE_DIR = "D:/sentinel_runs/myproject_run1"
sentinel assess --profile full-coverage D:/Git/my-target-repo
```

```bash
export SENTINEL_STATE_DIR="/tmp/sentinel_runs/myproject_run1"
sentinel assess --profile full-coverage /mnt/d/Git/my-target-repo
```

What this guarantees:

- **Operational scope** — the run targets *operational code* (a language-agnostic
  set: C#, Python, Java, TS/JS, Go, Rust, C/C++, …), not "all files." Config/data
  (`.xml`, `.json`, …), tests, helper scripts (`.vbs`, `.ps1`, …), and project/
  build files (`.csproj`, `.sln`, …) are excluded. Override with `--scope all`.
- **All skills, all agents** — `full-coverage` forces full specialist coverage,
  so a directed prompt can never silently narrow the skill set.
- **Fail-closed coverage gate** — the run **blocks** (does not silently finish) if
  less than 100% of in-scope operational source lines were reviewed by every
  required skill. Inspect `coverage_certificate.json` if the gate trips.

This single command is the intended way to hand the tool to another operator.

### Option A — Command Line (CLI)


Set environment variables then run directly. PowerShell (Windows):

```powershell
$env:SENTINEL_REPO_PATH = "D:/Git/my-target-repo"
$env:SENTINEL_STATE_DIR = "state_myproject_run1"
sentinel assess D:/Git/my-target-repo
```

Linux / WSL:

```bash
export SENTINEL_REPO_PATH="/mnt/d/Git/my-target-repo"
export SENTINEL_STATE_DIR="state_myproject_run1"
sentinel assess /mnt/d/Git/my-target-repo
```

Phase 1 will run through all discovery and review stages and then **pause at the Tech Debt Triage gate**.

### Option B — GitHub Copilot Chat (VS Code)

Sentinel ships with a `ChatSession` interface (`sentinel/chat/session.py`) designed for chat-native operation. This mode runs without requiring external API keys in your environment — Copilot's model handles inference.

**Setup:**

1. Open the `sentinel` folder in VS Code
2. Open GitHub Copilot Chat panel
3. Select your preferred model (GPT-5.1 or Claude Sonnet recommended)
4. Set `SENTINEL_CHAT_NATIVE_MODE=true` in your `.env`

**Startup probe** — paste this into Copilot Chat to confirm the session is ready:

```
Return exactly this single line and nothing else: COPILOT_OK
```

If you get `COPILOT_OK` back, the session is live. Then direct Copilot to drive Sentinel commands as needed, using the CLI commands below as your prompt vocabulary.

---

## Phase 1: Full Assessment Walkthrough

### 1. Start the assessment

```bash
sentinel assess /path/to/target-repo
```

Sentinel will:
- Discover all modules and files
- Map dependencies
- Run Quick Review (Layer 2a) on every module
- Auto-trigger specialist skills based on findings
- Synthesize a tech debt register
- Stop at the **Tech Debt Triage gate**

### 2. Review the findings

```bash
sentinel triage --list
```

Each item shows: ID, severity (Critical / P1 / P2 / P3), module, and description.

### 3. Make triage decisions

```bash
sentinel triage --approve TD-001    # include in remediation plan
sentinel triage --defer TD-002      # acknowledge, address later
sentinel triage --exclude TD-003    # out of scope, won't fix
```

### 4. Triage cross-repo dependency candidates (if any)

If Phase 1 discovers issues that span external repositories:

```bash
sentinel triage --list-dependencies
sentinel triage --include-dependency DEP-123 --dependency-rationale "Needed for approved fix" --dependency-owner "team-a"
sentinel triage --defer-dependency DEP-456 --dependency-rationale "Out of current release scope"
sentinel triage --exclude-dependency DEP-789 --dependency-rationale "Optional integration, skip"
```

### 5. Generate the remediation plan

```bash
sentinel assess --continue
```

This runs Stage 6 (Fix Planner — Layer 5 skill) and produces `plan.json`.

### 6. Review and approve the plan

```bash
sentinel plan --show
sentinel plan --approve
```

---

## Phase 2: Governed Remediation Walkthrough

### Start the remediation engine

```bash
sentinel govern --start
```

Tasks progress automatically through the state machine. The engine pauses at human gates.

### Working with tasks

```bash
sentinel task --list                                    # see all tasks and current states
sentinel task --show-diff TASK-001                      # review the diff before approving
sentinel task --approve TASK-001 --comment "LGTM"       # approve via GitHub PR flow
sentinel task --approve-local TASK-001 --comment "Ready"  # approve locally (no PR required)
sentinel task --request-changes TASK-001 --comment "Add regression coverage"
sentinel task --reject TASK-001 --comment "Needs better error handling"
sentinel task --sync TASK-001                           # sync PR state from GitHub
sentinel status                                         # overall system status
```

### Self-approve mode (single-operator or CI environments)

```bash
sentinel govern --start --self-approve
# or set in .env:
# SENTINEL_SELF_APPROVE=true
```

Removes the GitHub PR gate requirement. Tasks progress through local approval only.

### Resolving disputes

```bash
sentinel dispute --resolve TASK-001 --winner architect   # architect's position wins
sentinel dispute --resolve TASK-001 --winner coder       # coder's position wins
```

---

## Phase 3: Feature Enhancement Request (FER) Walkthrough

```bash
# 1. Submit a feature request (JSON payload)
sentinel fer --submit fer_request.json

# 2. Triage the FER
sentinel fer --triage FER-001 --approve

# 3. Run feasibility analysis (Layer 7a)
sentinel feasibility --run FER-001
sentinel feasibility --approve FER-001 --approach "incremental_rollout"

# 4. Generate the enhancement spec (Layer 7b)
sentinel spec --run FER-001
sentinel spec --approve FER-001

# 5. Tasks auto-enter Phase 2 queue — start governed remediation
sentinel govern --start
```

---

## Defect Assessment & Advisory Root Cause Analysis (RCA)

Sentinel supports targeted bug investigation and root cause analysis in an **advisory-first, diagnostic mode** — ideal when you need to diagnose issues and recommend solutions without direct repository write permissions or PR authority.

### Advisory Diagnostic Workflow

1. **Submit a bug report / stack trace**:
   ```bash
   sentinel defect --submit bug_report.json
   ```
2. **Review and triage the defect**:
   ```bash
   sentinel defect --list
   sentinel defect --triage DEF-001 --approve
   ```
3. **Run 3-Tier Root Cause Analysis (Layer 8a/8b)**:
   ```bash
   sentinel rca --run DEF-001
   ```
4. **Exported Deliverables**:
   - **Executive Decision Brief**: `reports/RCA_DEF-001_Report.md` detailing:
     - *Tier 1 (Proximal Mechanism)*: Immediate failure site / crash frame.
     - *Tier 2 (Systemic Factor)*: Missing guards, race conditions, or invariant breaches.
     - *Tier 3 (Root Architectural Cause)*: Core design or lifecycle contract violation.
     - Blast radius, remediation trade-offs, and acceptance criteria.
   - **Ready-to-Apply Git Patch**: `patches/DEF-001.patch` (standalone unified diff).
   - **Reproduction Test Stubs**: Ready-to-commit unit/integration test specifications.

---

## Quick-Run: Try It Against Sentinel Itself

The safest way to do a test run is to use a disposable git worktree on a throwaway branch so state commits don't land on `main`:

```powershell
Set-Location D:\Git\sentinel
git fetch origin
git worktree add ..\sentinel-phase1-demo -b demo/phase1 origin/main
Set-Location ..\sentinel-phase1-demo

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# Configure for local-only dry run
Copy-Item .env.example .env
# Edit .env: set SENTINEL_LOCAL_ONLY_MODE=true

.\.venv\Scripts\sentinel.exe assess .
.\.venv\Scripts\sentinel.exe triage --list
.\.venv\Scripts\sentinel.exe triage --approve TD-0001
.\.venv\Scripts\sentinel.exe assess --continue
.\.venv\Scripts\sentinel.exe plan --show
```

Clean up when done:

```powershell
Set-Location D:\Git\sentinel
git worktree remove ..\sentinel-phase1-demo
git branch -D demo/phase1
```

---

## Environment Reference

Copy `.env.example` to `.env`. Key settings:

| Variable | Default | Description |
|---|---|---|
| `SENTINEL_PROVIDER` | _(auto-detect)_ | `openai`, `anthropic`, `github-models`, or `copilot-cli` |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | _(empty)_ | Provider credentials |
| `GITHUB_TOKEN` + `SENTINEL_ALLOW_GITHUB_MODELS` | _(empty)_ / `false` | GitHub Models (opt-in) |
| `SENTINEL_MODEL` | `gpt-5.1` | Model name (default depends on provider) |
| `SENTINEL_LOCAL_ONLY_MODE` | `false` | Disable all external model calls |
| `SENTINEL_CHAT_NATIVE_MODE` | `false` | Enable VS Code Copilot Chat operation mode |
| `SENTINEL_REPO_PATH` | `.` | Path to the repository being assessed |
| `SENTINEL_STATE_DIR` | `sentinel/state` | Where JSON state files are written |
| `SENTINEL_MAX_TOKENS` | `8192` | Per-call token limit |
| `SENTINEL_CHUNK_MAX_LINES` | `300` | Max lines per code chunk sent to review |
| `SENTINEL_CHUNK_OVERLAP_LINES` | `80` | Overlap between chunks for context continuity |
| `SENTINEL_DEP_SCAN_LIMIT` | `500` | Max Python files sampled for dependency import context (Stage 2). Uses stratified sampling across directories — entry points (`__init__.py`, `main.py`, etc.) are always included first. Set `SENTINEL_STRICT_QUALITY_MODE=true` to scan all files with no cap. |
| `SENTINEL_STRICT_QUALITY_MODE` | `false` | Full-coverage mode: review every in-scope line (no chunk cap / sampling) and enable the coverage + quality-scorecard gates |
| `SENTINEL_REVIEW_PROFILE` | `reliability-first` | Specialist selection. `triggered` = Quick Review findings drive the full specialist suite; `reliability-first` = a fixed reliability subset (correctness, security, data-integrity, error-handling) always-on + concurrency when triggered |
| `SENTINEL_ENFORCE_FULL_SPECIALIST_COVERAGE` | `false` | Run every specialist on every chunk (heaviest; ignores Quick-Review triaging) |
| `SENTINEL_SELF_APPROVE` | `false` | Skip GitHub PR gate; use local approval only |
| `SENTINEL_ENFORCE_SINGLE_PR_APPROVER` | `false` | Restrict PR merges to a single named approver |
| `SENTINEL_REQUIRED_PR_APPROVER` | _(empty)_ | GitHub login of the required PR approver |
| `SENTINEL_LOG_LEVEL` | `INFO` | Logging verbosity |

---

## Architecture Reference

### Agent roles

| Agent | Responsibility |
|---|---|
| **Architect** | Plans, reviews, prioritizes, and approves TODOs and triage decisions |
| **Coder** | Submits TODOs, implements fixes, performs self-review |
| **QA** | Verifies each fix against the original finding and acceptance criteria |
| **Docs** | Updates documentation — mandatory gate before any PR is created |

Agents are **stateless** between API calls. Context is reconstructed from state files before every invocation. **Agents never self-approve** — every role transition requires a different agent or human confirmation.

### Skill system (32 skills across 9 layers, 0–8)

| Layer | Skills | Purpose |
|---|---|---|
| 0 | Cartographer, Dependency Analyzer, Scope Guardian, Architectural Checklist | Discovery and inventory |
| 1 | Review Prioritizer | Determines which specialists to activate |
| 2 | Quick Review (mandatory), Correctness, Security, Data Integrity, API Contracts, Trust Boundaries, Config Drift, Build/Release Integrity, Change Impact, Operational Risk | Core review |
| 3 | Performance, Maintainability, Testability, Error Handling, Concurrency | Specialist depth |
| 4 | Code Smell Detector, Refactoring Advisor | Structural quality |
| 5 | Fix Planner, Remediation Pattern Proposer | Remediation planning |
| 6 | Verification Reviewer, Cross-Repo Verification, Transition Manifest Generator | Fix validation |
| 7 | Feasibility Analyzer, Enhancement Spec Writer, Backwards Compatibility Checker | Feature intake |
| 8 | Root Cause Analyzer, Bug Remediation Spec Writer | Defect RCA & advisory specs |

### Orchestrator

The orchestrator (`sentinel/orchestrator/main.py`) is **pure deterministic Python** — no AI component. It is the sole writer of all JSON state files. It manages state transitions, assembles context for each agent call, and enforces all governance gates. This ensures governance logic can never be overridden by a model response.

### State persistence

All state lives in JSON files under the configured state directory. The `StateStore` commits state to git after each significant step (when the state dir is inside a repo), creating a full audit trail. Files include: `repositories.json`, `modules.json`, `dependencies.json`, `findings.json`, `semantic_groups.json`, `concept_themes.json`, `remediation_patterns.json`, `coverage_certificate.json`, `quality_scorecard.json`, `tech_debt.json`, `plan.json`, `tasks.json`, `reviews.json`, `disputes.json`, `audit_log.json`, `fers.json`, `feasibility.json`, `specs.json`, `pr_manifests.json`, `defects.json`, `rca_reports.json`, `defect_specs.json`.

---

## Project Structure

```text
sentinel/
  agents/         # Agent implementations (Architect, Coder, QA, Docs)
  api/            # Provider clients and model call abstraction
  assess/         # Phase 1 pipeline (cartographer, chunker, reviewer, synthesizer)
  chat/           # ChatSession — Copilot Chat / programmatic interface
  defect/         # Defect models, lifecycle manager, context slicer, and RCA orchestrator
  govern/         # Phase 2 governed remediation pipeline
  orchestrator/   # State machine, orchestration engine, state persistence
  phase3/         # FER, feasibility, and spec workflows
  reports/        # Decision briefs, handoffs, and RCA report/patch formatters
  skills/         # Skill loader and 32 skill prompt files
  cli.py          # Primary CLI entry point
  cli_defect.py   # Defect and RCA command registration
  cli_phase3.py   # Phase 3 command registration
  config.py       # Environment-backed configuration (all settings)
tests/            # Unit and integration tests (pytest + respx)
docs/             # Supporting documentation and example run artifacts
```

---

## Troubleshooting

### Dependency scan warning: `sampled N of M Python files`

This is expected for large repositories. Stage 2 uses a stratified sample across directories to build the AI's dependency analysis context. The actual dependency graph (`dependencies.json`) always includes every file — the sampling only affects the qualitative AI commentary.

To increase the sample size:

```env
SENTINEL_DEP_SCAN_LIMIT=1000   # raise from default 500
```

To scan every file with no cap:

```env
SENTINEL_STRICT_QUALITY_MODE=true
```

Note: scanning very large repos (10k+ files) in strict mode will send a large payload to the model and consume significantly more tokens.

---

## Development & Testing

```bash
# Run all tests
pytest

# Focused test runs
python -m pytest tests/test_assess.py tests/test_local_mode.py tests/test_chunk_cap.py

# Single test
python -m pytest tests/test_assess.py::TestReviewTelemetry::test_review_module_with_telemetry_uses_char_fallback_after_line_retry_fails

# Provider client tests only
pytest tests/test_client.py
```

Tests use `respx` for HTTP mocking and `pytest-asyncio` with `asyncio_mode = "auto"`. Coverage is enabled by default.

---

## Project Status

Actively developed. Current suite: **1500 passed / 1 skipped / 0 failed**. Recent hardening was validated via a live, full-coverage self-assessment of Sentinel's own codebase:

- **Chunking** tuned to 300/80 with AST-aware split points; a pin-and-crawl bug that over-split files (and a root-module re-glob) were fixed, removing a large review-call amplification.
- **Graceful degradation** — an out-of-contract `finding_kind` from the model is repaired (not crash-aborted) mid-run; the count is reported in `coverage_report.json`.
- **Quality scorecard** completeness now excludes no-source modules, so the strict-mode gates pass on a fully-reviewed codebase.
- **Specialist selection** is configurable via `SENTINEL_REVIEW_PROFILE` (`triggered` vs `reliability-first`).

---

## Notes

- Sentinel is built for **iterative, governed execution** — not fully autonomous merging. The default posture keeps all human review gates active.
- For reproducible behavior, pin your model in `.env` (`SENTINEL_MODEL=gpt-5.1`).
- State directories are gitignored by default. Use the dual-remote workflow to push project-specific state to a project repository.
- The safest first run is always `SENTINEL_LOCAL_ONLY_MODE=true` against a disposable worktree to validate discovery without spending model tokens.

