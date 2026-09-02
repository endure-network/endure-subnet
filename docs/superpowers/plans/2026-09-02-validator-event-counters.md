# Validator Event Counters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose three unlabelled Prometheus Counters for process-local validator RPC and explicit weight-submission failures.

**Architecture:** `AdaptiveRpcGate` remains the source of the two RPC totals and carries them through a replacement generation. `BaseValidatorNeuron` gains one in-memory counter for explicit unsuccessful SDK weight responses. `Validator.runtime_health()` projects these source values and the API renders them as Counter families in the existing per-scrape registry, without changing the nineteen Phase 1 Gauges.

**Tech Stack:** Python 3.12, FastAPI, `prometheus-client>=0.22,<0.24`, pytest, Pyright, Ruff.

**Spec:** `docs/specs/2026-09-02-validator-event-counters.md`

## Global Constraints

- Expose exactly `endure_validator_rpc_rate_limited_total`, `endure_validator_rpc_deferred_total`, and `endure_validator_weight_submissions_failed_total`, all unlabelled Prometheus Counters.
- Counters start at zero per validator process and remain monotonic until exit. Do not persist, restore, label, migrate, add dependencies, add dashboards/alerts/histograms, or implement #19/#24 late-completion counters.
- Count rate limits at provider classification, cooldown deferrals before send, and weight failures only at an explicit unsuccessful SDK response. Do not count ambiguous, timeout, skipped, successful, retry, health-read, or scrape events.
- Preserve all Phase 1 endpoint behavior, missing-value omission, and the existing 19 Gauge families.
- Before any push, run `ENDURE_ACTIVATION_LINEAGE_REF=upstream/staging make verify`. Do not push, open a PR, merge, or touch public repositories without explicit authorization.

---

## File structure

| File | Responsibility |
| --- | --- |
| `endure/base/rate_gate.py` | Source-owned RPC totals and replacement preservation. |
| `endure/base/validator.py` | Source-owned explicit failed-weight process total. |
| `neurons/validator.py` | Runtime-health projection of the three process totals. |
| `endure/api/app.py` | Prometheus Counter exposition beside unchanged Phase 1 Gauges. |
| `tests/base/test_rate_gate.py` | RPC event and replacement behavior. |
| `tests/base/test_base_validator.py` | Explicit failure boundary and excluded outcomes. |
| `tests/neurons/test_weight_emission_audit.py` | Process reset independent from durable audit history. |
| `tests/api/test_app.py` | Parser-backed Counter and endpoint contract tests. |

## Task 1: Lock source-event semantics

**Files:**

- Modify: `tests/base/test_rate_gate.py`
- Modify: `tests/base/test_base_validator.py`
- Modify: `endure/base/rate_gate.py`
- Modify: `endure/base/validator.py`

**Interfaces:**

- Consumes: `AdaptiveRpcGate.snapshot() -> RateGateSnapshot`, `AdaptiveRpcGate.replacement() -> AdaptiveRpcGate`, and `_submit_prepared_weights(attempt) -> WeightSubmissionResult`.
- Produces: replacement-stable RPC totals and `BaseValidatorNeuron._weight_submissions_failed_process_total: int`.

- [ ] **Step 1: Write failing tests for each source boundary**

```python
def test_replacement_preserves_process_rpc_totals() -> None:
    gate = AdaptiveRpcGate(clock=_Clock())
    with pytest.raises(RateLimited):
        gate.call(RpcPriority.ESSENTIAL, _provider_limited_operation)
    with pytest.raises(RateLimited):
        gate.call(RpcPriority.ESSENTIAL, lambda: None)

    replacement = gate.replacement()

    assert replacement.snapshot().rate_limited_total == 1
    assert replacement.snapshot().deferred_total == 1


def test_explicit_unsuccessful_weight_response_increments_process_total(
    validator: _ConcreteValidator,
) -> None:
    validator.subtensor.set_weights.return_value = _response(success=False)

    result = validator._submit_prepared_weights(_attempt())

    assert result.status == EMISSION_FAILED
    assert validator._weight_submissions_failed_process_total == 1
```

Add paired successful, `RateLimited`, and `ChainRpcStalled` cases that assert the weight total stays zero.

- [ ] **Step 2: Run the tests red**

```bash
.venv/bin/python -m pytest tests/base/test_rate_gate.py -k total -v
.venv/bin/python -m pytest tests/base/test_base_validator.py -k process_total -v
```

Expected: the weight-total tests fail because the field and increment boundary do not exist.

- [ ] **Step 3: Implement only the source state**

Initialize in `BaseValidatorNeuron.__init__`:

```python
self._weight_submissions_failed_process_total = 0
```

Immediately before returning the existing `EMISSION_FAILED` result from the explicit `not response.success` branch:

```python
self._weight_submissions_failed_process_total += 1
```

Do not alter the already source-owned RPC increments or `replacement()` copies.

- [ ] **Step 4: Run the tests green**

```bash
.venv/bin/python -m pytest tests/base/test_rate_gate.py -k total -v
.venv/bin/python -m pytest tests/base/test_base_validator.py -k process_total -v
```

- [ ] **Step 5: Commit**

```bash
git add endure/base/rate_gate.py endure/base/validator.py tests/base/test_rate_gate.py tests/base/test_base_validator.py
git commit -m "feat: count validator telemetry events"
```

## Task 2: Project process totals through runtime health

**Files:**

- Modify: `neurons/validator.py:203-316`
- Modify: `endure/api/app.py:73-105`
- Modify: `tests/neurons/test_weight_emission_audit.py`
- Modify: `tests/api/test_app.py`

**Interfaces:**

- Consumes: `gate.rate_limited_total`, `gate.deferred_total`, and `self._weight_submissions_failed_process_total`.
- Produces: required integer RuntimeHealth keys `rpc_rate_limited_process_total`, `rpc_deferred_process_total`, and `weight_submissions_failed_process_total`.

- [ ] **Step 1: Write a failing process-versus-durable test**

```python
def test_runtime_health_projects_process_totals_not_durable_history(
    storage: Storage,
) -> None:
    validator = _audit_validator(storage)
    _record_failed_weight_emission(storage)
    validator._weight_submissions_failed_process_total = 0
    validator.rpc_gate.snapshot.return_value = _gate_snapshot(
        rate_limited_total=2, deferred_total=3
    )

    health = validator.runtime_health()

    assert health["rpc_rate_limited_process_total"] == 2
    assert health["rpc_deferred_process_total"] == 3
    assert health["weight_submissions_failed_process_total"] == 0
```

Add a fresh-validator case with all three values at zero.

- [ ] **Step 2: Run the test red**

```bash
.venv/bin/python -m pytest tests/neurons/test_weight_emission_audit.py -k process_totals -v
```

Expected: FAIL because runtime health currently exposes the durable failed history rather than all three process totals.

- [ ] **Step 3: Add the projection**

Extend `RuntimeHealth` and return exactly:

```python
"rpc_rate_limited_process_total": gate.rate_limited_total,
"rpc_deferred_process_total": gate.deferred_total,
"weight_submissions_failed_process_total": (
    self._weight_submissions_failed_process_total
),
```

Remove the misleading durable `failed_weight_submissions_total` RuntimeHealth projection, but do not alter durable storage or its confirmation tests. Keep the nested `rpc_gate` mapping for Phase 1 readiness.

- [ ] **Step 4: Run focused tests green**

```bash
.venv/bin/python -m pytest tests/neurons/test_weight_emission_audit.py -k 'process_totals or confirmation_health' -v
.venv/bin/python -m pytest tests/api/test_app.py -k metrics -v
```

- [ ] **Step 5: Commit**

```bash
git add neurons/validator.py endure/api/app.py tests/neurons/test_weight_emission_audit.py tests/api/test_app.py
git commit -m "feat: project validator event totals"
```

## Task 3: Render Counter exposition without changing Phase 1 Gauges

**Files:**

- Modify: `endure/api/app.py:25-34,375-516`
- Modify: `tests/api/test_app.py`

**Interfaces:**

- Consumes: the three RuntimeHealth process-total keys from Task 2.
- Produces: canonical Counter families and `_total` samples at `/metrics`.

- [ ] **Step 1: Write a failing parser-backed endpoint test**

```python
def test_metrics_exposes_event_totals_as_unlabelled_counters(
    client: TestClient,
) -> None:
    response = client.get("/metrics")
    families = text_string_to_metric_families(response.text)
    by_name = {family.name: family for family in families}

    assert by_name["endure_validator_rpc_rate_limited"].type == "counter"
    assert by_name["endure_validator_rpc_deferred"].type == "counter"
    assert by_name["endure_validator_weight_submissions_failed"].type == "counter"
    assert all(
        sample.labels == {}
        for family in by_name.values()
        for sample in family.samples
    )
```

Set source values to `2`, `3`, and `1`; assert the `_total` samples carry those values. Retain the assertions for 19 Gauges and scrapeable degraded health.

- [ ] **Step 2: Run the endpoint test red**

```bash
.venv/bin/python -m pytest tests/api/test_app.py -k event_totals_as_unlabelled_counters -v
```

Expected: FAIL because the three Counter families are absent.

- [ ] **Step 3: Add the separate Counter rendering loop**

Import `Counter` alongside `Gauge`. Keep the current Gauge list and loop untouched. Add a separate tuple of valid source totals and render it in the fresh registry:

```python
Counter(
    "endure_validator_rpc_rate_limited",
    "Count of provider rate-limit responses observed by the validator process.",
    registry=registry,
).inc(value)
```

Use base names without `_total`; `prometheus-client` creates the canonical Counter sample. Omit absent or malformed values instead of zero-filling. Do not add labels or a global registry.

- [ ] **Step 4: Run API metrics tests green**

```bash
.venv/bin/python -m pytest tests/api/test_app.py -k metrics -v
```

- [ ] **Step 5: Commit**

```bash
git add endure/api/app.py tests/api/test_app.py
git commit -m "feat: expose validator event counters"
```

## Task 4: Verify reset, scope boundary, and handoff

**Files:**

- Modify: `tests/neurons/test_weight_emission_audit.py`
- Modify: `tests/api/test_app.py`
- Verify: all files above.

**Interfaces:**

- Consumes: source, runtime, and API behavior from Tasks 1–3.
- Produces: evidence for lab issue #4, not a public push or PR.

- [ ] **Step 1: Write reset and scope-exclusion tests**

```python
def test_new_validator_process_resets_event_counter_sources(storage: Storage) -> None:
    first = _audit_validator(storage)
    first._weight_submissions_failed_process_total = 4

    restarted = _audit_validator(storage)

    assert restarted.runtime_health()["weight_submissions_failed_process_total"] == 0


def test_metrics_does_not_expose_late_completion_totals(client: TestClient) -> None:
    rendered = client.get("/metrics").text

    assert "late_completions_total" not in rendered
    assert "late_set_weights_completions_total" not in rendered
```

- [ ] **Step 2: Run the focused regression tests**

```bash
.venv/bin/python -m pytest tests/neurons/test_weight_emission_audit.py -k resets_event_counter_sources -v
.venv/bin/python -m pytest tests/api/test_app.py -k late_completion_totals -v
```

- [ ] **Step 3: Correct only observed failures**

Keep process counters out of storage construction and restoration. If a duplicate increment appears, move the increment to its one source branch; do not deduplicate at the API. If durable history seeds a process total, remove that projection rather than adding persistence.

- [ ] **Step 4: Run the complete validation**

```bash
git diff --check upstream/develop...HEAD
ENDURE_ACTIVATION_LINEAGE_REF=upstream/staging make verify
```

Expected: source tests, API parser checks, all 1,302 tests, migrations, design-doc alignment, guardrails, public-release scan, gitleaks, duplication, and Pylint pass.

- [ ] **Step 5: Record private evidence**

Comment on lab issue #4 with the candidate SHA, focused/full commands, per-process reset semantics, and the explicit exclusion of #19/#24 metrics. Keep it open until a public contribution is merged. Do not push or open a public PR.

## Self-review

- Spec coverage: Tasks 1–2 cover source events and restart semantics; Task 3 covers Counter type, HELP/TYPE, labels, and unchanged Gauges; Task 4 covers reset, exclusions, verification, and private handoff.
- Placeholder scan: every implementation task has paths, interfaces, test commands, expected behavior, and a bounded commit.
- Type consistency: the three RuntimeHealth keys produced by Task 2 are exactly the keys read by Task 3; Counter base names intentionally produce the required public `_total` samples.

