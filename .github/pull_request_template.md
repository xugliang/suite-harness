## Summary

Describe the framework behavior changed and why the change belongs in SuiteHarness.

## Scope and security impact

- Runtime owner: Root / Tenant / Product / Agent-Session / none
- Capabilities added or changed:
- Customer360 or private-profile impact:
- Approval, budget, audit, lifecycle, or resource-cleanup impact:

## Verification

- [ ] Tests cover the intended behavior.
- [ ] Security-sensitive changes include a negative test proving the forbidden action does not occur.
- [ ] `python -m ruff check .` passes.
- [ ] `python -m pytest` passes on supported Python versions.
- [ ] `python -m build --outdir dist .` and `python scripts/check_distributions.py dist` pass.
- [ ] New public protocols or adapters document lifecycle, timeout, cancellation, idempotency, scope, and failure behavior.
- [ ] No secrets, customer data, runtime databases, evidence, or telemetry were committed.
- [ ] Public contracts and architecture documentation were updated when needed.

## Compatibility

List effects on public APIs, descriptors, manifests, configuration, or migrations. Write “none” if not applicable.
