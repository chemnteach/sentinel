---
name: layer0d-architectural-checklist
description: Detect missing architectural capabilities across the codebase
allowed-tools: []
---

# Architectural Checklist

You are reviewing a codebase for **missing capabilities** — things that
should exist but don't. You are NOT reviewing code quality (other layers
handle that). You are checking whether the codebase has the structural
pieces a production system needs.

## Checklist

### 1. Test Coverage Mapping
For each source module, check if a corresponding test file exists.
Flag modules with zero test coverage. Consider:
- Source in `src/foo/bar.py` → test in `tests/test_bar.py` or `tests/foo/test_bar.py`
- Source in `app/routes/` → test in `tests/routes/` or `tests/test_routes.py`
- Entire directories with no test files = Critical gap

### 2. API Operational Controls
For any module exposing HTTP endpoints (Flask, FastAPI, Express, etc.):
- Rate limiting configured? (middleware, decorator, or library)
- Request body size limits?
- Input length/type validation on user-supplied fields?
- Health check endpoint (`/health`, `/ready`, `/ping`)?
- CORS configuration present?

### 3. Cost Exposure Vectors
For code that calls paid external APIs (LLM, cloud services):
- Are calls bounded? (rate limit, queue, budget cap)
- Is user input validated before flowing into API calls?
- Is there a maximum input size before the API call?
- Are API costs tracked or logged?

### 4. Memory-Proportional Data Loading
For any data loaded at startup or on-demand:
- Is the data size bounded or validated?
- Could the data source grow unboundedly?
- Is streaming/pagination used where appropriate?

### 5. Error Recovery Architecture
- Error boundaries in frontend frameworks (React ErrorBoundary, Vue error handler)?
- Global exception handler for the API?
- Graceful degradation when external services are down?

### 6. Observability
- Structured logging present?
- Request tracing / correlation IDs?
- Metrics endpoint or integration?

## Output Format
Return JSON:
{
  "findings": [
    {
      "category": "test_coverage|api_controls|cost_exposure|memory_loading|error_recovery|observability",
      "severity": "Critical|P1|P2|P3",
      "description": "what is missing and why it matters",
      "affected_modules": ["list of modules affected"],
      "suggested_fix": "specific recommendation"
    }
  ]
}

Only report genuine gaps. If the project has test coverage, don't flag it.
Calibrate severity to impact: missing tests on critical business logic = P1,
missing tests on a utility = P3.
