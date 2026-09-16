# 运行停止与会话恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 SuiteHarness 增加经授权的指定运行停止、取消历史恢复和只读 ReAct 安全续跑。

**Architecture:** SessionStore 记录运行级停止意图和完整恢复点；Runner 生成内部 checkpoint，并只从可信恢复点在新运行中接续。ChannelApplication 负责终态围栏、显示历史和续跑资格；EnterpriseChannelGateway 与 Web/飞书适配器只负责重新鉴权及协议转换。

**Tech Stack:** Python 3.11+、ABC、Pydantic 2、asyncio、SQLite/WAL、pytest、ruff。

**Spec:** `docs/superpowers/specs/2026-09-16-run-interruption-and-recovery-design.md`

## Global Constraints

- 旧 `run_id` 取消后永不重新运行；安全续跑必须是显式新 `run_id`，关联旧运行。
- 控制操作使用与发消息相同的认证、产品路由、会话成员/客户 ACL；默认销售画像不共享。
- 停止意图先持久化，跨进程可观察；取消不得写成功回答或 MD 长期记忆。
- 仅复用完整、成功、内联且有精确底座工具身份的 READ observation；排除 `agent.delegate`、`suiteharness.result.read`、Bash、MCP 和插件工具。
- 恢复点之后有未完成工具调用、未知写工具状态、来源水位变化、运行 uncertain 或 TTL 过期时 fail closed。
- 实现阶段不读取、停止、修改或部署业务服务器；源码发布另行授权，保留已有测试临时文件。
- 实现行为一律 RED→GREEN→重构，使用独立短 `--basetemp` 路径，不重用现有用户测试目录。

---

### Task 1: 持久运行停止意图

**Files:**
- Modify: `src/suiteharness/sessions/models.py`, `src/suiteharness/sessions/protocols.py`, `src/suiteharness/sessions/sqlite.py`, `src/suiteharness/sessions/memory.py`, `src/suiteharness/sessions/__init__.py`
- Test: `tests/test_run_stop_store.py`

**Interfaces:**
- `RunStopOutcome = REQUESTED | ALREADY_REQUESTED | ALREADY_FINISHED | NOT_FOUND`。
- `SessionStore.request_stop(identity: SessionIdentity, *, run_id: str) -> RunStopOutcome`：只有未过期的 `active` claim 接受；重复请求幂等。
- `SessionStore.stop_requested(identity: SessionIdentity, *, run_id: str) -> bool`。
- `SessionStore.active_run_ids(identity: SessionIdentity, *, limit: int = 4) -> tuple[str, ...]`：仅未过期 active claims；飞书无参数命令仅当结果恰好一个时停止。

- [ ] Step 1: 在真实 SQLite 双连接与内存实现上写失败测试：claim 后 stop 被另一实例读到；错身份 NOT_FOUND；重复 ALREADY_REQUESTED；finalizing/过期 ALREADY_FINISHED；查询只返回活跃且身份相同的 run。
- [ ] Step 2: 选择尚不存在的任务专用临时目录，用 `pytest -q tests/test_run_stop_store.py -o pythonpath=src --basetemp <任务临时目录>` 看到因 API 缺失失败。
- [ ] Step 3: SQLite 建独立 `session_run_stops` 表（完整 session key + run_id，FK claim），在 BEGIN IMMEDIATE 中核对声明 state/expiry 后 INSERT OR IGNORE；内存实现沿同规则；不占用长会话锁。
- [ ] Step 4: 重跑本任务测试及已有 `tests/test_session_migration_race.py`、`tests/test_channel_stream.py`，确认停止与当前 claim fencing 共存。
- [ ] Step 5: 仅提交本任务文件，记录 RED/GREEN 命令与结果。

### Task 2: Runner 内部安全恢复点

**Files:**
- Modify: `src/suiteharness/execution/models.py`, `src/suiteharness/execution/events.py`, `src/suiteharness/execution/runner.py`, `src/suiteharness/execution/protocols.py`, `src/suiteharness/agents/react.py`, `src/suiteharness/runtime/kernel.py`
- Test: `tests/test_run_resume_runner.py`, `tests/test_execution_stream.py`（仅调整与内部 checkpoint 顺序冲突的旧断言）

**Interfaces:**
- `RunResumeState` 为冻结的 Pydantic 结构：`source_run_id`, `workflow_state`, `observations`, `iterations_used`, `tool_calls_used`, `call_ids`, `prompt_digest`, `route_key`；`RunRequest.resume` 为 optional。
- `RunEvent.kind='checkpoint'` 为内部事件，`data['resume_state']` 包含可序列化 `RunResumeState`、原 `input`、`conversation`；不会向 Web 直接发送。
- `Workflow.resume_key(request, frame, prompt) -> str | None` 默认 `None`；`ReActWorkflow` override 为路线/模型配置 key，且仅受信任的 `RunResumeState.source_run_id` 可验证旧 request identity。
- `ActivatedCustomerBundle.cancel(scope, run_id) -> bool` 仅委派底座 Runner。

- [ ] Step 1: 写失败测试：模型等待时 Runner 发布初始检查点；完整白名单 READ 工具 batch 后检查点含 observation；新 run 仅从旧 observation 生成标准 tool message，不再次调用旧工具；旧 call_id 不能重复；恢复时错误 route/prompt、非 read_only、结果引用、delegate/写工具均拒绝。
- [ ] Step 2: 选择尚不存在的任务专用临时目录，运行 `pytest -q tests/test_run_resume_runner.py -o pythonpath=src --basetemp <任务临时目录>` 确认缺失功能失败。
- [ ] Step 3: 在 Runner 渲染原始 prompt 后计算去除运行 ID 元数据的 digest；仅 ReAct 在第一次模型轮次前及完整白名单工具批次后发布有上限的内部 checkpoint。恢复 RunRequest 从新 grant 开始，但继承已完成 observations/预算计数；继续之后的新工具调用仍由原 Runner 权限、网络及 read_only 路径执行。底座安全集合只含 fs.list/read/glob/grep、web.search/fetch，精确校验 identity.namespace=`suiteharness`、origin=`suiteharness.builtin.fs`/`suiteharness.builtin.web`、version=`1` 及 effects；`agent.delegate` 不可进入集合。
- [ ] Step 4: 重跑本任务测试和 `tests/test_execution_stream.py`、`tests/test_multiagent_cancellation.py`、`tests/test_multiagent_react.py`。
- [ ] Step 5: 仅提交本任务文件，记录 RED/GREEN 命令与结果。

### Task 3: 独立持久恢复点

**Files:**
- Modify: `src/suiteharness/sessions/models.py`, `src/suiteharness/sessions/protocols.py`, `src/suiteharness/sessions/sqlite.py`, `src/suiteharness/sessions/memory.py`, `src/suiteharness/sessions/__init__.py`
- Test: `tests/test_run_resume_store.py`

**Interfaces:**
- `RunResumePoint(identity, run_id, payload: JsonValue, memory_invalidated_revision, memory_source_versions, created_at)`；payload 含旧 message digest、原输入/上下文、Runner 恢复状态，审批凭据不得持久化。
- `SessionStore.save_resume_point(identity, point) -> RunResumePoint | None`：仅 active 且未要求停止的声明可存；容量超限/不安全点返回 `None`，不影响原运行。
- `SessionStore.get_resume_point(identity, *, run_id: str) -> RunResumePoint | None`：身份精确匹配，短 TTL（默认 300 秒）。

- [ ] Step 1: 写失败测试：SQLite reopen 读取完整只读 observation；不同销售/客户或运行不可读取；停止后/过期/大于 512 KiB/含 approval token 不保存；覆盖时仅最新完整点可见。
- [ ] Step 2: 选择尚不存在的任务专用临时目录，运行 `pytest -q tests/test_run_resume_store.py -o pythonpath=src --basetemp <任务临时目录>` 确认失败。
- [ ] Step 3: 建独立 `session_run_resume_points` 表并以完整 session key + run_id 保持一条最新点，写入和 claim/stop 检查在同一 BEGIN IMMEDIATE；内存实现同语义，严格检查 JSON 和 TTL。
- [ ] Step 4: 重跑 store 测试及 `tests/test_session_migration_race.py`；提交本任务文件。

### Task 4: 应用运行控制和续跑判定

**Files:**
- Modify: `src/suiteharness/server/application.py`, `src/suiteharness/channels/gateway.py`, `src/suiteharness/server/host.py`
- Test: `tests/test_run_control_application.py`

**Interfaces:**
- `ChannelApplication.stop_message(scope, *, target_message_id: str | None) -> RunStopOutcome`：显式目标或唯一活跃 run；使用稳定 run ID 计算且不获取执行锁。
- `ChannelApplication.history(..., include_cancelled: bool = False)`：默认行为不变；显式展示 CANCEL partial/incomplete。
- `ChannelApplication.continue_task(message, scope, *, target_message_id: str) -> AsyncIterator[OutboundEvent]`：校验目标旧 run 的 CANCEL 终态/最新性/来源水位/恢复点/TTL 后为当前 `message.message_id` 创建新运行。
- Gateway 的 `stop_authenticated`, `history_authenticated`, `continue_authenticated` 复用 `_authorize` 和 scope 构造，不给客户端接受任何 scope/恢复 payload；host-owning gateway subclass 对控制入口执行与普通 dispatch 相同的 closed guard。

- [ ] Step 1: 写失败测试：跨进程止住无 delta 的模型；同销售正确客户可停/读/续，另一销售和共享会话非成员不可；取消与成功竞态不保存成功或长期记忆；初始 checkpoint 可重启模型轮次，完整只读结果可续跑；旧运行 idempotent replay 不重执行；写/委派 started 后拒绝续跑；终态 transcript/checkpoint 崩溃窗不误判 uncertain。
- [ ] Step 2: 选择尚不存在的任务专用临时目录，用 `pytest -q tests/test_run_control_application.py -o pythonpath=src --basetemp <任务临时目录>` 确认失败。
- [ ] Step 3: 应用在 stream 消费处隐藏内部 checkpoint、持久化安全点，跟踪 checkpoint 后 TOOL_STARTED；后台查询停止意图并调用 `activation.cancel`，终态提交前再次核验；Gateway 抽取现有准入/scope 构造供所有控制操作复用。
- [ ] Step 4: 完整执行 `tests/test_channel_stream.py`、`tests/test_memory_runtime.py`、`tests/test_multiagent_cancellation.py` 和本任务测试；提交本任务文件。

### Task 5: Web 标准协议

**Files:**
- Modify: `src/suiteharness/channels/dispatch.py`, `src/suiteharness/channels/websocket/models.py`, `src/suiteharness/channels/websocket/server.py`, `src/suiteharness/channels/websocket/__init__.py`
- Test: `tests/test_websocket_run_control.py`

**Interfaces:**
- Client `stop{stop_id,target_client_message_id,conversation_id,product_id?}`、`history{history_id,conversation_id,product_id?,limit}`、`continue{client_message_id,target_client_message_id,conversation_id,product_id?}`。
- Server `stop_result{stop_id,outcome}`、`history_result{history_id,events}`；continue 使用普通 ACK/EventFrame，且和普通 message 共用连接并发上限。
- Dispatcher ABC 可选方法默认 `NotImplementedError`，旧产品适配器不必实现。

- [ ] Step 1: 写失败测试：完整 parse 严格校验目标 ID；同 socket 两条活跃仅停目标、跨重连凭认证会话停目标、重复 stop 幂等、历史/续跑的错误客户拒绝、后续消息仍可用。
- [ ] Step 2: 选择尚不存在的任务专用临时目录，运行 `pytest -q tests/test_websocket_run_control.py -o pythonpath=src --basetemp <任务临时目录>` 确认失败。
- [ ] Step 3: 控制帧在接收循环直接调用已认证 Dispatcher；stop 不占消息并发槽且不按诊断任务名称取消；history/continue 由 gateway 重验 ACL，输出按现有 frame 上限约束。
- [ ] Step 4: 重跑本任务测试及 `tests/test_websocket_tasks.py`、`tests/test_channel_stream.py`；提交本任务文件。

### Task 6: 飞书停止命令与文档

**Files:**
- Modify: `src/suiteharness/channels/feishu/processor.py`, `src/suiteharness/channels/dispatch.py`, `docs/module-guide.zh-CN.md`, `docs/security-model.md`
- Test: `tests/test_feishu_run_stop.py`

**Interfaces:**
- `/停止` 仅停止同作用域唯一活跃运行；`/停止 <provider-message-id>` 指定运行。应用/网关返回带简短中文 `output` 的出站 COMPLETED（拒绝则 FAILED），由只接收终态的官方飞书 sink 发给用户。命令不进入模型对话和长期记忆。

- [ ] Step 1: 写失败测试：验签后命令停止目标，不同员工/群线程不能停错目标；多活跃时无参数拒绝；重复飞书事件仍由 deduplicator 控制；普通文本路径不变。
- [ ] Step 2: 选择尚不存在的任务专用临时目录，运行 `pytest -q tests/test_feishu_run_stop.py -o pythonpath=src --basetemp <任务临时目录>` 确认失败。
- [ ] Step 3: processor 在 normalize 后识别固定语法并调用已认证 Dispatcher 控制入口；更新中文模块说明和安全边界。
- [ ] Step 4: 重跑本任务测试及 `tests/test_feishu_official.py`；提交本任务文件。

### Task 7: 集成验证及最终审查

**Files:**
- Test: `tests/test_run_control_application.py`, `tests/test_run_resume_runner.py`, `tests/test_run_resume_store.py`, `tests/test_websocket_run_control.py`, `tests/test_feishu_run_stop.py`

- [ ] Step 1: 用本地可用环境执行所有 SuiteHarness 测试、`ruff check src tests`、`git diff --check`，记录未安装可选依赖的环境限制而不擅自 pip 安装。
- [ ] Step 2: 独立 reviewer 核对设计约束、各层边界、竞态、授权及结果重放，修复 Critical/Important 后重跑覆盖测试。
- [ ] Step 3: 报告本地分支/commit、真实测试结果及尚未在 7.22 和产品 B 私有 UI 上验证的事项；不自动推送或部署。
