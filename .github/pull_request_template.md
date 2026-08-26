## Summary
<!-- 1-3 bullet points. What does this PR do, and why? -->

## Spec reference
<!-- Link to the relevant current spec section. Use docs/specs/2026-07-06-alpha-risk-v1-scope.md for Alpha Risk changes. If no protocol spec applies, say so. -->

## Checklist
- [ ] `make verify` passes locally
- [ ] `make verify-ci` passes locally if guardrails, migrations, or coverage-sensitive code changed
- [ ] New/changed logic has tests
- [ ] No `float` for risk/economic values (Decimal only)
- [ ] No placeholders without `NotImplementedError("spec §X.Y — ...")`
- [ ] Public functions are type-hinted; `pyright` clean
- [ ] Spec section refs preserved in docstrings/comments
- [ ] Protocol version contract updated if protocol/assessment/aggregation/scoring/publication semantics changed
- [ ] Migration impact reviewed (`make migrations`) if storage or Alembic files changed

## Test plan
<!-- How was this tested? Unit tests, localnet, mock mode, manual? -->

## Compatibility impact
<!-- Note any CLI, protocol, storage, deployment, or operator-facing change. Write "none" when no compatibility surface changes. -->
