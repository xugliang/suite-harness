# Controlled Multi-Agent Design

## Decision and scope

SuiteHarness shall add optional, framework-owned agent delegation. Product bundles are not themselves multi-agent. The first release supports one parent agent delegating at most two bounded, same-product child runs and receiving their outputs as ordinary tool observations. It does not add peer-to-peer messaging, arbitrary subprocess launch, cross-product delegation, detached jobs, or a new required method on existing product interfaces. Multi-agent is disabled unless a product and a trusted channel grant explicitly opt in.

This document captures the design approved in the conversation on 2026-09-15. It builds on the existing `Workflow` ABC, product strategies, `ExecutionRunner`, `RequestScope`, tool registry, grant authority, channel policy, and audit journal. The runner remains non-replaceable and enforces every child tool call.

## Ownership and boundaries

Framework owns `AgentRoleDefinition`, role registry, `DelegationService` ABC with a denying default, durable `AgentTaskStore`, scheduling, child scope derivation, bounded results, budget accounting, cancellation tree, and audit events. Product code contributes role-specific prompt/workflow strategy, task decomposition and result synthesis, and business resource authorizers. Deployment configuration chooses role availability, model route, tool identity allowlist, and caps. Product A and B are not obliged to implement a new interface unless they opt in.

The model-facing `agent.delegate` schema contains only a registered `role_id` and a bounded task. It cannot choose a provider/model, command, cwd, executable, arbitrary team, product, tenant, principal, approval token, or grant. The tool returns a structured status and bounded output through a standard tool message. It never exposes chain-of-thought.

## Child identity and authorization

The trusted service creates a fresh `run_id` and child `agent_id`/`session_id` linked to the authenticated parent run. Tenant, product, principal, channel, roles, request/correlation IDs, and `read_only` are inherited; child scope is an agent scope under the same product. No child inherits the user's chat session or writes its internal transcript to the user-facing session. Only authorized input references or a bounded snapshot are supplied.

Child tool authorization is the intersection of the parent's currently valid grant, the configured role allowlist, and the deployment policy, with fresh expiry and exact tool identities. It must never manufacture a broader grant. The existing runner's read-only check, tool effects, company channel policy, egress/sandbox checks, and call-scoped approval remain authoritative. Feishu delegation cannot gain write access when the parent is read-only; Web approval binds to the same authenticated principal and the actual child tool call. Product B's customer ownership check is still required before customer data enters a delegated task.

## Scheduling, state, and budgets

State is `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, or `TIMED_OUT`, stored durably with parent and child run IDs, principal, product, role, timestamps, and bounded result/failure. An idempotency key derived from `(tenant, product, principal, parent_run_id, tool_call_id)` prevents duplicate spawn. Unknown or foreign task IDs are denied. Startup must not silently rerun an uncertain running child; it marks it interrupted/failed and permits a new explicitly authorized request.

First release runs children concurrently in the existing product service with a shared parent deadline, at most two children, depth one, bounded parallelism, and an aggregate iteration/tool-call budget across parent and children. Budget use is accounted for before dispatch, not inferred from only the parent `agent.delegate` call. Cancellation of the parent cancels every active child and finalizes their states. A child failure is a structured tool observation; it is never disguised as an answer.

## Stream and product adoption

The parent remains responsible for the final answer. WebSocket can display sanitized child started/completed/progress events, but the ordinary final answer path remains available for Feishu. Child tool outputs and citations are delivered to the parent model as standard tool messages; no `web_search_results` prompt injection shortcut.

Initial acceptance uses an artificial test product plus an opt-in B scenario such as independent product search and customer-specific communication advice. A stays single-agent unless an A workflow has a demonstrable need. Per-company deployment and separately deployed product containers remain unchanged; a child is a logical run, not a container per agent.

## Acceptance criteria

1. With delegation disabled, existing A/B runs and the published core regression suite behave unchanged.
2. An enabled parent can delegate two configured same-product roles and synthesize a final answer from their tool observations.
3. Cross-product, cross-principal, cross-customer, unauthorized role/tool, write-from-read-only, and foreign task-result attempts fail closed.
4. Aggregate quotas, timeout, cancellation, duplicate call, child failure, and service restart have deterministic terminal states without orphan execution or duplicate side effects.
5. The audit trail can correlate parent and child run IDs without storing secrets or chain-of-thought; streaming conveys progress without leaking child private context.
