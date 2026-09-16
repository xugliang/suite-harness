# Controlled Multi-Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional same-product parent-to-child agent delegation through the trusted SuiteHarness runner, with durable task state, narrowed child grants, aggregate limits, and standard ReAct tool observations.

**Architecture:** A protected root `agent.delegate` tool calls a host-owned `DelegationService`. The service resolves configured roles, derives child scope, narrows the current parent grant, records a child task, and re-enters the active customer bundle with role-specific prompt and the ordinary `ExecutionRunner`. Existing products stay single-agent unless the deployment enables roles and grants the delegation tool.

**Tech Stack:** Python 3.11+, Pydantic 2, asyncio, SQLiteDatabase, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-15-controlled-multi-agent-design.md`

## Global Constraints

- First release is parent-to-child only; no arbitrary subprocess launch, detached tasks, peer messaging, or cross-product delegation.
- Default disabled; existing product `Workflow` ABC and A/B code must not gain required methods.
- Maximum two children per parent, depth one; share a deadline and aggregate iteration/tool-call budget.
- Parent grant, configured role, and deployment policy form an exact-identity intersection; child is never more privileged, and inherits parent `read_only`.
- Child re-enters the fixed `ExecutionRunner`; Web approval and Feishu policy stay in force.
- All new tests use `PYTHONPATH=src` and a project-local `--basetemp` on Windows. Linux-only sandbox behavior is verified separately.

---

### Task 1: Strict opt-in roles and registry

**Files:**
- Create: `src/suiteharness/multiagent/roles.py`, `src/suiteharness/multiagent/__init__.py`
- Modify: `src/suiteharness/config/models.py`, `src/suiteharness/config/__init__.py`
- Test: `tests/test_multiagent_roles.py`

**Interfaces:**
- Produce `AgentRoleDefinition(role_id, instruction, allowed_tools, model_route)` and `MultiAgentProductConfig(enabled, roles, max_children=2, max_depth=1, max_parallel=2)`, `MultiAgentConfig(products={})`.
- Produce `ConfiguredAgentRoleRegistry.resolve(product_id: str, role_id: str) -> AgentRoleDefinition | None` and `policy(product_id) -> MultiAgentProductConfig | None`; disabled products return `None`.

- [ ] **Step 1: RED.** Test that old config validates with `multi_agent` absent and a disabled product resolves no role; test duplicate roles, unselected product, unconfigured model route, and forbidden `agent.delegate` child tool are rejected.

```python
assert ConfiguredAgentRoleRegistry(MultiAgentConfig()).resolve("sales-profile", "search") is None
with pytest.raises(ValueError):
    MultiAgentProductConfig(enabled=True, roles=(search_role, search_role))
```

- [ ] **Step 2: Verify RED.** Run `python -m pytest -q tests/test_multiagent_roles.py --basetemp .pytest-tmp-multiagent-roles`; expect missing role/config classes, not test setup failure.
- [ ] **Step 3: GREEN.** Add frozen, validated role/config classes and selected-product/route checks. `allowed_tools` names are declarative ceilings; no grant is issued here.
- [ ] **Step 4: Verify GREEN and refactor.** Re-run the focused test and `tests/test_abc_foundations.py`; then `python -m ruff check` on touched files.
- [ ] **Step 5: Commit.** `git add` only Task 1 files; commit `feat: configure optional agent roles`.

### Task 2: Durable owned child-task state

**Files:**
- Create: `src/suiteharness/multiagent/tasks.py`, `src/suiteharness/multiagent/sqlite.py`
- Test: `tests/test_multiagent_tasks.py`

**Interfaces:**
- Produce `AgentTaskStatus` enum, immutable `AgentTaskRecord`, `AgentTaskStore(ABC)` with `create`, `get_owned`, `transition`, `cancel_children`, `recover_interrupted`, and `SQLiteAgentTaskStore(SQLiteDatabase)`.
- `create` atomically returns the existing same-scope record for a repeated `(tenant, product, principal, parent_run_id, call_id)`; it rejects the same key with changed role/task digest.
- `transition` is a compare-and-set from one allowed state to another; a terminal record cannot be overwritten.

- [ ] **Step 1: RED.** Test idempotency, foreign-principal lookup denial, terminal CAS, cancellation fence, and recovery of uncertain `RUNNING` records against `SQLiteDatabase(":memory:")`.

```python
first = await store.create(parent_scope, "run-a", "call-a", "search", "digest-a")
same = await store.create(parent_scope, "run-a", "call-a", "search", "digest-a")
assert same.task_id == first.task_id
assert await store.get_owned(other_principal_scope, first.task_id) is None
```

- [ ] **Step 2: Verify RED.** Run `python -m pytest -q tests/test_multiagent_tasks.py --basetemp .pytest-tmp-multiagent-tasks`; expect missing store/contract behavior.
- [ ] **Step 3: GREEN.** Install static `multiagent.tasks` schema version 1 through `SQLiteDatabase.install_schema` and implement transactions through `database.transaction(write=...)`. Never put child-task tables in the session DB.
- [ ] **Step 4: Verify GREEN and refactor.** Re-run focused tests and `tests/test_session_migration_race.py`; run touched-file ruff.
- [ ] **Step 5: Commit.** Commit only Task 2 files as `feat: persist scoped agent tasks`; Task 4 publishes their package exports.

### Task 3: Aggregate budget and trusted child execution

**Files:**
- Create: `src/suiteharness/multiagent/budget.py`, `src/suiteharness/multiagent/service.py`
- Modify: `src/suiteharness/execution/protocols.py`, `src/suiteharness/execution/runner.py`, `src/suiteharness/runtime/kernel.py`
- Test: `tests/test_multiagent_delegation.py`, `tests/test_multiagent_budget.py`

**Interfaces:**
- Consume role registry and task store from Tasks 1/2.
- Produce `DelegationService(ABC).delegate(context: ToolCallContext, role_id: str, task: str) -> JsonValue` and `DisabledDelegationService` that rejects; `ControlledDelegationService` receives a trusted activation, tool registry, grant authority, store and roles.
- Produce `AggregateRunBudget` with atomic `take_iteration()`, `take_tool_slots(n)`, remaining deadline, and child-count reservation; parent and child runner share the same object.
- Extend `ToolCallContext` with optional trusted `grant_id`, `read_only`, `aggregate_budget`, and cancellation signal, defaulted to preserve existing handler construction.

- [ ] **Step 1: RED.** Use an actual `ExecutionRunner`, tool registry and grant authority to test that a child receives only role/parent-intersected identities, same principal/channel/product and read-only state; invalid role and cross-product are denied. Test that parallel child tool calls consume one aggregate pool and cancellation leaves no active child.

```python
assert await budget.take_iteration() is True
assert await budget.take_tool_slots(1) == 1
assert await budget.take_tool_slots(99) == 0
```

- [ ] **Step 2: Verify RED.** Run the two focused multiagent tests; expect missing service/budget or an incorrect authorization/limit assertion.
- [ ] **Step 3: GREEN.** Add the pool to the runner's existing loop without weakening per-run ceilings; supply it to the trusted tool context. The service resolves the parent grant again, derives `ProductContext.agent`, issues a fresh grant for exact allowed identities with expiry no later than parent expiry, records the task before running, uses role prompt as a separate system message, and calls a trusted activation child-stream entry that still validates scope/lease and runs the fixed runner. Revoke the child grant and finalize task in `finally`.
- [ ] **Step 4: Verify GREEN and refactor.** Re-run focused tests plus `tests/test_abc_execution.py` and `tests/test_execution_stream.py`; run touched-file ruff.
- [ ] **Step 5: Commit.** Commit only Task 3 files as `feat: run bounded delegated agents`.

### Task 4: Protected model-facing tool and host opt-in

**Files:**
- Create: `src/suiteharness/multiagent/tool.py`
- Modify: `src/suiteharness/server/foundation.py`, `src/suiteharness/server/host.py`, `src/suiteharness/multiagent/__init__.py`
- Test: `tests/test_multiagent_react.py`, `tests/test_multiagent_host.py`

**Interfaces:**
- Consume `ControlledDelegationService` from Task 3.
- Produce root/protected `agent.delegate` with strict JSON schema: required `role_id`, `task`; no model/cwd/command/principal/product/grant arguments. Register it only for configured enabled roles. It appears to the model only when the route's exact grant template includes the identity.

- [ ] **Step 1: RED.** Test disabled config has no exposed delegate tool; enabled role plus explicit grant permits one standard ReAct tool call, followed by a `tool` message and a final model answer. Test forbidden schema fields and ungranted tool denial.

```python
assert {tool.name for tool in frame.available_tools} == {"agent.delegate"}
assert model_messages[-1].role is ModelRole.TOOL
```

- [ ] **Step 2: Verify RED.** Run `tests/test_multiagent_react.py tests/test_multiagent_host.py`; expect absent tool/host wiring.
- [ ] **Step 3: GREEN.** Install via `InMemoryToolRegistry.register_protected`; inject only a narrow service handler. Wire the task store and service in Foundation/host, and document the required explicit channel/product grant template. Do not auto-add delegation to legacy A/B grants.
- [ ] **Step 4: Verify GREEN and refactor.** Re-run focused tests, `tests/test_abc_integrations.py`, `tests/test_channel_stream.py`, and ruff.
- [ ] **Step 5: Commit.** Commit Task 4 files as `feat: expose controlled agent delegation tool`.

### Task 5: Documentation and release verification

**Files:**
- Modify: `docs/product-authoring.md`, `docs/security-model.md`, `README.md`
- Test: complete framework test suite, role/task/delegation/ReAct tests, Linux sandbox smoke where available.

**Interfaces:** Document product role opt-in, explicit route grant, customer ownership, prompt/tool ownership, A unchanged, B optional pilot, and operational caps; no code interface created.

- [ ] **Step 1: RED/contract review.** Review spec acceptance criteria against Task 1-4 tests; add a missing behavioral regression test before any code fix.
- [ ] **Step 2: GREEN/docs.** Add a minimal same-product B role example without changing B customer ownership or issuing new B grants by default.
- [ ] **Step 3: Verify.** Run focused tests, Windows-safe core regression, `python -m ruff check src tests`, and on Linux run the full suite and sandbox smoke if the task-owned environment is safely available. Report pre-existing platform failures separately from new failures.
- [ ] **Step 4: Review and commit.** Obtain spec/quality review of the branch diff, address load-bearing findings, commit documentation, and leave GitHub push/server cutover for an explicit deployment decision.
