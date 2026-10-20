"""Plain-language explainers for supported finding_kinds in the Decision Brief.

Curated (not model-generated) so every row of a given kind reads consistently
and a reviewer who does NOT know the codebase can still understand the issue
class, why it is risky, and what to check. Keyed by registry finding_kind.

Each entry: what / why / problems (list) / check / optional title + risky/safer
sketch. Every supported finding kind used by report generation must have
explainer cluster metadata, and missing coverage is treated as an error during
registry validation.
"""
from __future__ import annotations

from sentinel.assess.finding_kinds import SUPPORTED_REPORT_FINDING_KINDS

EXPLAINERS: dict[str, dict] = {
    "error_signal_loss": {
        "title": "Error signal loss",
        "what": "The code hits a real error but never surfaces it — the exception is caught and ignored, or a failure is turned into a normal-looking return value (None, empty, False-as-success) — so callers believe the operation succeeded.",
        "why": "Everything downstream keeps running on a false assumption of success, and the original failure disappears. The symptom then shows up much later, far from its cause.",
        "problems": [
            "Silent data corruption or loss",
            "Operations that 'succeed' without actually doing anything",
            "Bugs that are extremely hard to trace because the error was hidden at the source",
            "Real failures that never trigger logs, metrics, or alerts"
        ],
        "check": "For each swallowed failure, decide whether it should instead be raised, logged, or returned as an explicit error rather than hidden.",
        "risky": "try:\n    save(record)\nexcept Exception:\n    pass            # failure vanishes; caller thinks it saved",
        "safer": "try:\n    save(record)\nexcept StorageError as e:\n    log.error('save failed for %s', record.id, exc_info=e)\n    raise            # or return an explicit error result"
    },
    "silent_data_loss": {
        "title": "Silent data loss",
        "what": "A path that is supposed to durably write or update data can partially succeed, drop, or corrupt that data without anyone being told it failed.",
        "why": "Persistence is a promise. If a write half-completes or is discarded silently, the system's stored state diverges from what users and other code believe is true — and nothing flags it.",
        "problems": [
            "Lost or partially-written records that look complete",
            "State that is internally inconsistent (one file updated, a related one not)",
            "Data-loss incidents discovered only when someone notices missing data later"
        ],
        "check": "Confirm multi-step writes are all-or-nothing (transaction / atomic replace) and that any discard of data is deliberate and logged.",
        "risky": "open(path, 'w').write(serialize(data))   # crash mid-write leaves a truncated file",
        "safer": "tmp = path + '.tmp'\nwrite(tmp, serialize(data))\nos.replace(tmp, path)                     # atomic: old file intact until full write"
    },
    "contract_violation": {
        "title": "Contract violation",
        "what": "Code breaks a documented or implied agreement — a precondition, a postcondition, a return-value promise, or an interface's expected behavior.",
        "why": "Other code is written trusting that contract. When one side quietly breaks it, callers do the wrong thing even though each piece looks correct in isolation.",
        "problems": [
            "Callers crash or misbehave on inputs/outputs the contract said were impossible",
            "Bugs that only appear at the boundary between two components",
            "Fixes in one place re-break another that depended on the old behavior"
        ],
        "check": "Verify the value/shape actually returned (or required) matches what the function's name, type hints, and docstring promise.",
        "risky": "def get_user(id) -> User:   # promises a User\n    if not found: return None  # but can return None",
        "safer": "def get_user(id) -> User | None:   # contract states the None case\n    ...                              # and callers handle it"
    },
    "logic_bug": {
        "title": "Logic bug",
        "what": "The control flow, a boundary (off-by-one, empty/zero/negative), an assumption, or a value-handling step is simply wrong, so the code computes the wrong result — without it being a broken contract.",
        "why": "The program runs fine but does the wrong thing. These are the classic correctness defects that tests and reviewers exist to catch.",
        "problems": [
            "Incorrect output, miscounts, wrong branches taken",
            "Edge cases (empty input, last element, error path) handled incorrectly",
            "Behavior that is right for the common case but wrong at the margins"
        ],
        "check": "Trace the specific scenario in the finding — especially boundaries and the assumptions the code makes about its inputs."
    },
    "timeout_handling_gap": {
        "title": "Timeout handling gap",
        "what": "A call to a dependency or a long-running operation has no time bound, or doesn't handle timing out correctly — so it can hang or behave unpredictably when the other side is slow.",
        "why": "Anything that crosses a boundary (network, disk, subprocess, another service) can stall. Without a bound and a defined timeout behavior, a slow dependency becomes a hang in your code.",
        "problems": [
            "Requests or jobs that hang indefinitely and tie up resources",
            "Cascading slowdowns when one slow dependency stalls everything waiting on it",
            "Retries or partial work left in an unclear state after a timeout"
        ],
        "check": "Confirm external/long operations have a timeout, and that the timeout path leaves the system in a clean, defined state.",
        "risky": "resp = httpx.get(url)              # no timeout -> can hang forever",
        "safer": "resp = httpx.get(url, timeout=10)  # bounded; handle httpx.TimeoutException"
    },
    "concurrency_update_risk": {
        "title": "Concurrency update risk",
        "what": "A shared piece of state is read-then-written in a way that isn't atomic, so two actors updating it at the same time can duplicate, lose, or corrupt the update.",
        "why": "When updates aren't atomic, interleaving operations from different threads/processes/requests can overwrite each other — the classic lost-update race.",
        "problems": [
            "Lost updates (one writer silently overwrites another)",
            "Duplicated or corrupted records under load",
            "Intermittent bugs that only reproduce under concurrency and are hard to debug"
        ],
        "check": "Confirm the read-modify-write is protected (lock, transaction, atomic/compare-and-set) wherever two actors can touch the same state.",
        "risky": "n = store.get('count'); store.set('count', n + 1)  # two writers -> one increment lost",
        "safer": "with lock: n = store.get('count'); store.set('count', n + 1)   # or atomic increment"
    },
    "hidden_dependency": {
        "title": "Hidden dependency",
        "what": "The code reaches out to time, randomness, the environment, the network, the disk, or global state directly and implicitly, instead of receiving it as a parameter.",
        "why": "Implicit dependencies make behavior depend on invisible context. The code can't be reasoned about or tested in isolation, and it behaves differently across environments for non-obvious reasons.",
        "problems": [
            "Tests that are flaky or impossible to write without real clocks/network/files",
            "Behavior that changes between machines or runs for hidden reasons",
            "Tight coupling to global state that makes refactoring risky"
        ],
        "check": "Decide whether the dependency (clock, random, env var, client) should be injected/parameterized so behavior is explicit and testable.",
        "risky": "def expires():\n    return datetime.now() > deadline   # hidden clock; can't test",
        "safer": "def expires(now):\n    return now > deadline               # clock passed in"
    },
    "observability_gap": {
        "title": "Observability gap",
        "what": "Failure paths lack the logging, metrics, tracing, or alerts you'd need to even notice a problem happened, let alone diagnose it.",
        "why": "If a failure isn't recorded, it's invisible. You can't fix — or even know about — problems you can't see in production.",
        "problems": [
            "Outages or errors that go unnoticed until users complain",
            "Incidents that take far longer to diagnose for lack of signal",
            "No way to measure how often a failure path is actually hit"
        ],
        "check": "Confirm important failure/edge paths emit enough signal (log + metric/alert) to detect and diagnose them."
    },
    "algorithmic_scaling_gap": {
        "title": "Algorithmic scaling gap",
        "what": "The algorithm or data structure chosen is fine for small inputs but scales worse than the real data size or load will demand (e.g., a nested loop that's quadratic over a list that grows).",
        "why": "Code that's instant on test data can become a bottleneck or hang in production once the input grows — performance cliffs are easy to miss in review.",
        "problems": [
            "Severe slowdowns or timeouts as data/usage grows",
            "Memory blowups on large inputs",
            "A feature that works in the demo and falls over at scale"
        ],
        "check": "Estimate the realistic input size and confirm the approach (lookups, batching, streaming) holds up there.",
        "risky": "for a in items:\n    if a in big_list:   # list membership is O(n) -> O(n^2) overall",
        "safer": "seen = set(big_list)\nfor a in items:\n    if a in seen:       # set membership is O(1)"
    },
    "crypto_misuse": {
        "title": "Cryptographic misuse",
        "what": "Cryptographic primitives, keys, nonces, randomness, or certificate validation are used in an unsafe way (e.g., a predictable random source, a reused nonce, or disabled cert checks).",
        "why": "Crypto only protects you when used exactly right; subtle misuse silently removes the protection while everything still 'works'.",
        "problems": [
            "Secrets or sessions that are guessable/forgeable",
            "Encryption that provides far less protection than assumed",
            "Disabled TLS verification opening the door to interception"
        ],
        "check": "Verify keys/nonces/randomness come from secure sources and that certificate/signature validation is not weakened."
    },
    "security_misconfiguration": {
        "title": "Security misconfiguration",
        "what": "A security-relevant setting is configured to an insecure value — default or hard-coded credentials, debug mode left on in production, overly permissive CORS, disabled TLS/certificate verification, exposed admin or management endpoints, or weak crypto settings in config.",
        "why": "The code may be correct, but the configuration quietly removes a protection. Insecure defaults and 'just to get it working' settings are a leading cause of breaches because they ship unnoticed and apply everywhere the config is loaded.",
        "problems": [
            "Default/weak credentials or debug endpoints that hand attackers an easy way in",
            "Disabled TLS verification or permissive CORS opening the door to interception or cross-origin abuse",
            "Verbose error/debug output leaking internals in production"
        ],
        "check": "Confirm security-relevant settings (auth, TLS verification, CORS, debug flags, exposed endpoints, crypto parameters) are set to safe values for the deployment environment, not left at insecure defaults.",
        "risky": "DEBUG = True            # in production\nVERIFY_SSL = False      # disables certificate checks",
        "safer": "DEBUG = env.bool('DEBUG', False)   # off unless explicitly enabled\nVERIFY_SSL = True                  # never disable cert validation"
    },
    "structural_complexity": {
        "title": "Structural complexity",
        "what": "A piece of code is broadly hard to understand and change safely — too long, too tangled, too many branches — even when no single named smell fits.",
        "why": "Complexity is where bugs hide and where changes go wrong. The harder code is to hold in your head, the more likely a fix introduces a new defect.",
        "problems": [
            "Higher bug rate and longer time to make changes",
            "Reviewers can't confidently reason about correctness",
            "Fear-driven 'don't touch it' code that ossifies"
        ],
        "check": "Decide whether the unit should be broken into smaller, single-purpose pieces with clearer names and fewer branches."
    },
    "change_verification_gap": {
        "title": "Change verification gap",
        "what": "A change here would touch enough code (large blast radius) that the existing seams and tests aren't sufficient to verify it's safe.",
        "why": "If you can't cheaply prove a change didn't break something, every change is a gamble. This is a *risk-to-modify* signal, not a bug per se.",
        "problems": [
            "Changes that silently break behavior with no test to catch it",
            "Slow, fearful development because nothing confirms correctness",
            "Regressions discovered in production instead of CI"
        ],
        "check": "Consider adding seams/tests around this area before (or as part of) any remediation so changes become verifiable."
    },
    "god_class": {
        "title": "God class",
        "what": "One class or module has accumulated too many responsibilities and coordination roles — it does, and knows about, far too much.",
        "why": "When everything funnels through one place, that place becomes a bottleneck for understanding, testing, and change, and tends to collect bugs.",
        "problems": [
            "Almost every change has to touch this one file",
            "Hard to test because it pulls in everything",
            "Unrelated concerns interfere with each other inside it"
        ],
        "check": "Decide whether distinct responsibilities can be extracted into focused classes/modules."
    },
    "divergent_change": {
        "title": "Divergent change",
        "what": "A single class or module changes for multiple unrelated reasons — different kinds of future change all land in the same place.",
        "why": "It signals the unit has more than one responsibility. Each unrelated reason-to-change is a separate concern that should ideally live separately.",
        "problems": [
            "Edits for one concern risk breaking an unrelated one",
            "Harder to reason about what a change affects",
            "Merge conflicts and coupling between unrelated features"
        ],
        "check": "Decide whether the differing reasons-to-change should be separated into distinct units."
    },
    "shotgun_surgery": {
        "title": "Shotgun surgery",
        "what": "One logical change forces you to touch many separate files, classes, or decision points scattered across the codebase.",
        "why": "The opposite of a god class: a single concern is smeared everywhere, so it's easy to update some sites and miss others.",
        "problems": [
            "Easy to forget a site, leaving the change half-applied (a real bug source)",
            "Every change is expensive and error-prone",
            "Behavior drifts as copies diverge over time"
        ],
        "check": "Decide whether the scattered logic can be consolidated behind one place that all sites call."
    },
    "inappropriate_intimacy": {
        "title": "Inappropriate intimacy",
        "what": "One module or class reaches into another's internals or crosses an intended abstraction boundary, instead of going through its public interface.",
        "why": "It couples the two together: changing the 'owner's' internals now silently breaks the 'intruder'. Boundaries exist so each side can evolve independently.",
        "problems": [
            "Internal refactors break distant code that reached in",
            "Two units that must now change together",
            "Erosion of the abstraction that made the design understandable"
        ],
        "check": "Decide whether the access should go through a proper public interface instead of internals."
    },
    "primitive_obsession": {
        "title": "Primitive obsession",
        "what": "A meaningful domain concept is passed around as loose primitives (strings, ints, dicts) instead of a dedicated type or value object.",
        "why": "Primitives carry no rules. Validation and meaning get duplicated (or forgotten) at every use site, and it's easy to mix up values of the same primitive type.",
        "problems": [
            "Same validation re-implemented inconsistently everywhere",
            "Easy to pass the wrong string/int in the wrong slot",
            "Meaning lives in people's heads instead of the type"
        ],
        "check": "Decide whether the concept deserves a small type/value object that centralizes its rules."
    },
    "data_clump": {
        "title": "Data clump",
        "what": "The same group of values keeps traveling together (e.g., always passing host, port, and timeout side by side) without being modeled as one concept.",
        "why": "When values always move together, they're really one thing. Keeping them loose duplicates the grouping everywhere and makes signatures noisy and error-prone.",
        "problems": [
            "Long parameter lists that are easy to misorder",
            "Changes to the group ripple across many signatures",
            "A missing field in one of the many copies"
        ],
        "check": "Decide whether the recurring group should become a single object/struct."
    },
    "sensitive_data_exposure": {
        "title": "Sensitive data exposure",
        "what": "Secrets, credentials, or personal data end up somewhere they shouldn't — written to logs, returned in error messages or responses, stored in plaintext, or hard-coded into the source.",
        "why": "Once a secret lands in a log line, an error payload, or source control, it's effectively public to anyone with access — and you usually can't fully claw it back; the value has to be rotated.",
        "problems": [
            "Credentials or personal data captured in logs, traces, or crash reports",
            "Secrets committed to source control that must be rotated once exposed",
            "Privacy/compliance violations and a much wider blast radius if any one system is breached"
        ],
        "check": "Confirm sensitive values are redacted from logs and errors, never hard-coded, and stored/transmitted only through approved secret-handling paths.",
        "risky": "log.info('auth ok user=%s token=%s', user, token)   # token now sits in the logs forever",
        "safer": "log.info('auth ok user=%s', user)                    # never log the secret itself"
    },
    "long_parameter_list": {
        "title": "Long parameter list",
        "what": "A function or constructor takes so many parameters that callers struggle to get them right — easy to misorder, hard to read, and a sign the routine is juggling too much.",
        "why": "Long signatures are error-prone, especially when several arguments share a type and can be swapped silently. They usually hint that some parameters belong together as one object, or that the function is doing more than one job.",
        "problems": [
            "Callers pass arguments in the wrong order with no error",
            "Hard to read call sites and to add or remove a parameter safely",
            "Often travels with a data clump that really wants to be its own type"
        ],
        "check": "Decide whether related parameters should be grouped into a single object, or whether the function should be split into smaller ones.",
        "risky": "def make_conn(host, port, user, pw, timeout, retries, ssl, verify): ...  # 8 positional args",
        "safer": "def make_conn(endpoint: Endpoint, opts: ConnOptions): ...               # grouped into objects"
    },
    "feature_envy": {
        "title": "Feature envy",
        "what": "A method is more interested in another object's data than its own — it repeatedly reaches into another class to pull fields and do work that really belongs over there.",
        "why": "The behavior is sitting in the wrong place. Logic and the data it operates on should live together; keeping them apart couples the two classes and scatters related rules across the codebase.",
        "problems": [
            "Changes to the other class's internals ripple back into this method",
            "Related logic ends up split across two places instead of one",
            "Weak cohesion — the method doesn't really belong to the class it lives in"
        ],
        "check": "Decide whether the behavior should move to the class that owns the data it keeps reaching for."
    },
    "api_contract_mismatch": {
        "what": "A caller and a provider disagree on the shape, type, or semantics of an interface.",
        "why": "Silent mismatches cause runtime failures that are hard to trace back to the contract break.",
        "problems": [
            "Unexpected nulls or type errors at runtime",
            "Failures only visible under specific inputs",
            "Cascading breakage when the provider changes"
        ],
        "check": "Compare the caller's assumptions against the provider's actual signature and documented contract."
    },
    "authorization_gap": {
        "what": "An operation that should require permission can be performed without it.",
        "why": "Missing authorization checks are a common source of privilege escalation and data exposure.",
        "problems": [
            "Unauthorized users can access or modify data",
            "Audit trails are incomplete",
            "Compliance requirements may be violated"
        ],
        "check": "Verify every sensitive operation checks the caller's identity and permissions before executing."
    },
    "blocking_io_in_async": {
        "what": "A synchronous blocking call (file, network, sleep) is made inside an async context.",
        "why": "Blocking the event loop stalls all other coroutines on the same thread, degrading throughput under load.",
        "problems": [
            "Latency spikes under concurrent requests",
            "Unresponsive services",
            "Timeouts on unrelated operations"
        ],
        "check": "Replace blocking calls with async equivalents (e.g. asyncio.sleep, aiofiles, async HTTP clients).",
        "risky": "async def fetch():\n    time.sleep(1)\n    return requests.get(url)",
        "safer": "async def fetch():\n    await asyncio.sleep(1)\n    return await client.get(url)"
    },
    "build_release_integrity_gap": {
        "what": "The build or release process has a gap that could allow unverified or tampered artifacts to be deployed.",
        "why": "A compromised build pipeline can ship malicious code even when the source is clean.",
        "problems": [
            "Unverified dependencies",
            "Unsigned artifacts",
            "Reproducibility failures",
            "Supply chain risk"
        ],
        "check": "Verify dependency pinning, artifact signing, and that the build process is fully reproducible from source."
    },
    "cache_correctness_gap": {
        "what": "Cached data can become stale or inconsistent with the source of truth.",
        "why": "Serving stale data silently is often worse than a cache miss — callers receive wrong answers with no indication.",
        "problems": [
            "Stale reads after writes",
            "Cache poisoning",
            "Inconsistent state across nodes",
            "Hard-to-reproduce bugs"
        ],
        "check": "Verify cache invalidation logic covers all write paths and that TTLs are appropriate for the data's change rate."
    },
    "concurrency_safety_gap": {
        "what": "Shared mutable state is accessed from multiple threads or coroutines without adequate synchronization.",
        "why": "Race conditions are non-deterministic and often only surface under load or specific timing.",
        "problems": [
            "Data corruption",
            "Intermittent failures",
            "Undefined behavior",
            "Security vulnerabilities from TOCTOU races"
        ],
        "check": "Identify all shared mutable state and verify each access is protected by an appropriate synchronization primitive."
    },
    "configuration_drift": {
        "what": "Configuration values differ between environments in ways that cause different behavior.",
        "why": "Code that works in development can fail silently in production due to undetected config differences.",
        "problems": [
            "Environment-specific bugs",
            "Security settings disabled in production",
            "Hard-to-reproduce issues"
        ],
        "check": "Audit configuration loading to ensure all required values are validated at startup and differences between environments are explicit."
    },
    "data_constraint_mismatch": {
        "what": "Data written to storage violates a constraint the schema or downstream system expects.",
        "why": "Constraint violations either fail loudly at write time or produce corrupt data that fails later at read time.",
        "problems": [
            "Runtime exceptions on reads",
            "Silent data truncation",
            "Referential integrity violations",
            "Downstream parsing failures"
        ],
        "check": "Verify all data written to storage satisfies the schema constraints before the write, not just at the call site."
    },
    "idempotency_gap": {
        "what": "An operation that should be safe to retry produces different results when called more than once.",
        "why": "Non-idempotent operations cause duplicate effects when retried after transient failures.",
        "problems": [
            "Duplicate records",
            "Double charges",
            "Repeated notifications",
            "Inconsistent state after retry storms"
        ],
        "check": "Verify the operation produces the same result on repeated calls with the same input, or that callers are protected from duplicates."
    },
    "injection_risk": {
        "what": "Untrusted input is incorporated into a command, query, or template without sanitization.",
        "why": "Injection is consistently the highest-severity class of vulnerability — it allows attackers to execute arbitrary commands or access unauthorized data.",
        "problems": [
            "SQL injection",
            "Command injection",
            "Template injection",
            "Data exfiltration",
            "Full system compromise"
        ],
        "check": "Verify all external input is parameterized or sanitized before being incorporated into queries, commands, or templates. Never use string concatenation for query construction."
    },
    "insecure_deserialization": {
        "what": "Data from an untrusted source is deserialized in a way that could execute arbitrary code or corrupt application state.",
        "why": "Deserialization of attacker-controlled data is a code execution vector in many languages.",
        "problems": [
            "Remote code execution",
            "Privilege escalation",
            "Denial of service via malformed payloads"
        ],
        "check": "Avoid deserializing data from untrusted sources using formats that support arbitrary object instantiation (e.g. pickle, Java serialization). Use JSON or protobuf with schema validation instead."
    },
    "migration_hazard": {
        "what": "A schema or data migration has risks that could cause data loss, downtime, or inconsistency.",
        "why": "Migrations that seem safe in isolation can fail destructively against production data volumes or under concurrent load.",
        "problems": [
            "Data loss on rollback",
            "Table locks causing downtime",
            "Inconsistent state if migration is partially applied"
        ],
        "check": "Verify the migration is reversible, tested against production-scale data, and safe to run under live traffic (or that a maintenance window is planned)."
    },
    "missing_transaction_boundary": {
        "what": "A sequence of related writes is not wrapped in a transaction, leaving the system in a partially-updated state if any step fails.",
        "why": "Partial writes create inconsistent state that is often impossible to recover from without manual intervention.",
        "problems": [
            "Orphaned records",
            "Inconsistent balances or counts",
            "Data that passes no integrity check",
            "Hard-to-diagnose corruption"
        ],
        "check": "Identify all multi-step write sequences and verify they are wrapped in a transaction that rolls back atomically on failure.",
        "risky": "db.insert(order)\ndb.update(inventory)\ndb.insert(audit_log)",
        "safer": "with db.transaction():\n    db.insert(order)\n    db.update(inventory)\n    db.insert(audit_log)"
    },
    "n_plus_one_query": {
        "what": "A loop executes one query per item in a collection instead of fetching all items in a single query.",
        "why": "N+1 queries scale linearly with data size, causing severe performance degradation as collections grow.",
        "problems": [
            "Response times that grow linearly with record count",
            "Database saturation under normal load",
            "Timeouts on large datasets"
        ],
        "check": "Identify loops that contain database calls and replace with batch queries, joins, or eager loading."
    },
    "partial_failure_handling_gap": {
        "what": "When part of a multi-step operation fails, the failure is not detected or handled, leaving the operation in an unknown state.",
        "why": "Partial failures are the hardest class of failure to handle correctly — the system is neither fully succeeded nor fully failed.",
        "problems": [
            "Silent data inconsistency",
            "Operations that appear to succeed but are incomplete",
            "No retry path for the failed portion"
        ],
        "check": "Verify that each step of a multi-step operation checks the result of the previous step and handles partial failure explicitly."
    },
    "trust_boundary_gap": {
        "what": "Data crosses a trust boundary (external input, inter-service call, user-supplied value) without being validated or sanitized.",
        "why": "Trusting data from outside a controlled boundary is the root cause of injection, privilege escalation, and data corruption vulnerabilities.",
        "problems": [
            "Malformed data corrupting internal state",
            "Injection attacks",
            "Privilege escalation via crafted inputs"
        ],
        "check": "Identify every point where data enters the system from outside a trust boundary and verify it is validated against an explicit schema before use."
    }
}


# Per-finding_kind "why these findings cluster" narratives, shown in the
# Decision Brief between the finding count and the patterns section. Curated
# (reviewed once) so a reviewer understands why findings of a given type tend
# to co-occur in the same module. Merged into each EXPLAINERS entry below as a
# "cluster" field; coverage of every registered finding_kind is enforced by
# tests/test_reports_decision_brief.py.
_CLUSTERS: dict[str, str] = {
    "logic_bug": "These findings share a module that contains logic operating on the same data structures or control flow paths. A bug in shared logic tends to manifest across multiple call sites in the same module.",
    "change_verification_gap": "These findings share a change surface — functions or classes that are modified together and lack tests that verify the change. The gap compounds when the same module owns multiple related behaviors with no test boundary between them.",
    "structural_complexity": "These findings cluster in modules where complexity has accumulated over time — long files, deeply nested logic, or functions with too many responsibilities. Complexity in one place tends to create complexity in adjacent code.",
    "error_signal_loss": "These findings share error-handling paths that suppress or swallow exceptions. Modules that handle multiple external dependencies tend to accumulate error signal loss across those boundaries.",
    "hidden_dependency": "These findings share implicit coupling — code that relies on global state, singletons, or side effects rather than explicit parameters. Hidden dependencies in one part of a module tend to spread to adjacent functions.",
    "contract_violation": "These findings share an interface boundary where callers and providers have drifted apart. Contract violations cluster when a module is a hub that many other modules depend on.",
    "timeout_handling_gap": "These findings share external call sites — network calls, database queries, or inter-service calls — without consistent timeout handling. Modules that orchestrate external calls accumulate timeout gaps across those calls.",
    "divergent_change": "These findings share a module that has grown to serve multiple unrelated purposes. When a module is changed for many different reasons, divergent change findings cluster because the same file is the catch-all for unrelated concerns.",
    "api_contract_mismatch": "These findings share an integration point where multiple callers make assumptions about the same provider. Contract mismatches cluster when a module is a shared dependency with an evolving interface.",
    "authorization_gap": "These findings share an entry point or handler layer where authorization should be enforced. Authorization gaps cluster when a module exposes multiple operations with inconsistent permission checks.",
    "blocking_io_in_async": "These findings share an async module that mixes blocking and non-blocking calls. Once a module adopts async, blocking calls tend to appear wherever developers reach for familiar synchronous APIs.",
    "build_release_integrity_gap": "These findings share a build or dependency configuration that has accumulated gaps. Build integrity issues cluster when the same configuration file governs multiple dependencies or build steps.",
    "cache_correctness_gap": "These findings share a caching layer where multiple code paths write to the same cache without consistent invalidation. Cache correctness gaps cluster when the cache is shared across many call sites.",
    "concurrency_safety_gap": "These findings share mutable state that is accessed from multiple execution contexts. Concurrency gaps cluster in modules that manage shared resources or coordinate multiple workers.",
    "configuration_drift": "These findings share configuration loading or defaulting logic that differs across environments. Configuration drift clusters when a module owns multiple settings with inconsistent validation.",
    "data_constraint_mismatch": "These findings share a data access layer where multiple write paths produce values that violate the same schema constraint. Constraint mismatches cluster when the same model or table is written from many places.",
    "idempotency_gap": "These findings share operations that are called from retry loops or message queues. Idempotency gaps cluster in modules that handle distributed or asynchronous work where retries are expected.",
    "injection_risk": "These findings share input-handling code that processes external data. Injection risks cluster in modules that are entry points for user input or external messages.",
    "insecure_deserialization": "These findings share deserialization paths that handle data from untrusted sources. They cluster in modules that parse external formats or inter-service messages.",
    "migration_hazard": "These findings share a migration or schema evolution path. Migration hazards cluster when a module owns multiple schema changes that interact with the same data.",
    "missing_transaction_boundary": "These findings share a data access module where multiple write operations lack transactional protection. Transaction boundary gaps cluster when the same module orchestrates multi-step writes.",
    "n_plus_one_query": "These findings share a data access layer that loads related objects in loops. N+1 queries cluster in modules that handle collections or relationships without batch loading.",
    "observability_gap": "These findings share code paths that handle significant operations without logging or metrics. Observability gaps cluster in modules that are critical but were written before observability was a concern.",
    "partial_failure_handling_gap": "These findings share multi-step operations where intermediate failures are not handled. Partial failure gaps cluster in modules that orchestrate sequences of dependent operations.",
    "trust_boundary_gap": "These findings share an entry point where external data enters the system. Trust boundary gaps cluster at module edges that receive input from outside the controlled perimeter.",
    "security_misconfiguration": "These findings share a configuration or initialization path where security settings are applied. Misconfigurations cluster when the same module owns multiple security-relevant settings.",
    "sensitive_data_exposure": "These findings share code paths that handle sensitive data — credentials, PII, or tokens. Exposure risks cluster in modules that are responsible for authentication or data access.",
    "concurrency_update_risk": "These findings share mutable state that is read and written in separate steps without atomic protection. Update risks cluster in modules that manage counters, balances, or other values updated concurrently.",
    "silent_data_loss": "These findings share write paths where data can be discarded without warning. Silent data loss clusters in modules that transform or filter data before persisting it.",
    "crypto_misuse": "These findings share cryptographic operations that use weak algorithms or incorrect parameters. Crypto misuse clusters in modules responsible for encryption, hashing, or signing.",
    "god_class": "These findings identify classes that have grown to own too many responsibilities. God classes cluster when a module has become the default home for new functionality over time.",
    "feature_envy": "These findings share methods that operate more on another class's data than their own. Feature envy clusters when a module is tightly coupled to a neighbor that it should delegate to.",
    "shotgun_surgery": "These findings share a concern that is scattered across many files. Shotgun surgery clusters when a cross-cutting concept has no single home and changes ripple through the module.",
    "primitive_obsession": "These findings share code that uses primitive types where domain objects would be clearer. Primitive obsession clusters in modules that handle domain concepts without modeling them explicitly.",
    "long_parameter_list": "These findings share functions with too many parameters. Long parameter lists cluster in modules that act as coordinators, passing configuration or context through many layers.",
    "data_clump": "These findings share groups of data that always appear together but are not modeled as a unit. Data clumps cluster when the same set of related values is passed through multiple functions in the same module.",
    "inappropriate_intimacy": "These findings share pairs of classes that access each other's internals. Inappropriate intimacy clusters when two modules have evolved together without maintaining clear boundaries.",
    "algorithmic_scaling_gap": "These findings share algorithmic choices that do not scale with input size. Scaling gaps cluster in modules that process collections or perform repeated lookups on growing data sets.",
}

def _merge_explainers_with_clusters(
    explainers: dict[str, dict],
    clusters: dict[str, str],
    supported_kinds: set[str] | frozenset[str],
) -> dict[str, dict]:
    """Attach cluster metadata and fail if any supported kind is uncovered."""

    merged = {kind: dict(metadata) for kind, metadata in explainers.items()}
    for kind, cluster in clusters.items():
        if kind in supported_kinds:
            merged.setdefault(kind, {})["cluster"] = cluster

    missing_supported_kinds = sorted(
        kind for kind in supported_kinds if "cluster" not in merged.get(kind, {})
    )
    if missing_supported_kinds:
        raise ValueError(
            "Missing explainer cluster metadata for supported finding kinds: "
            + ", ".join(missing_supported_kinds)
        )

    return merged


EXPLAINERS = _merge_explainers_with_clusters(
    EXPLAINERS,
    _CLUSTERS,
    SUPPORTED_REPORT_FINDING_KINDS,
)

__all__ = ["EXPLAINERS", "_merge_explainers_with_clusters"]
