# 清理当前会话

`/clear` 是认证后的确定性会话生命周期命令，不交给模型理解，不作为 LLM 工具暴露。仅独立一条 `/clear`（允许首尾空白、无附件）触发；提及命令、引用命令或带任意参数都不会清理。

范围取自可信 RequestScope 的 tenant/product/agent/session/owner。当前只允许发送者清理其自己拥有的会话；共享会话的代管清理不启用。飞书原消息不会撤回，Web 消费者可按 `conversation_cleared` 标志重置展示。

## 数据库实际变化

- `transcript_events.payload_json` 擦除为 JSON null，保留事件身份、序号、版本、时间及原内容摘要用于防重放。
- **所有** `session_checkpoints.workflow_state_json` 擦除为 JSON null，包括旧终态缓存和摘要，不只清最新快照。
- `sessions` 保留完整身份、单调版本及来源版本，推进记忆失效水位。
- `session_run_claims` 保留运行 ID，标记 abandoned，使旧消息不能重新执行业务副作用。
- `session_clear_receipts` 仅保存清理版本、计数、运行 ID 和完成/失败层；不含聊天正文。未完成清理阻止普通对话，重复原清理请求可以恢复。
- `tool_results` 中同会话运行的正文记录被删除；`cleared_result_owners` 保存 owner 哈希，拒绝旧运行迟到写入。
- 配置了 Markdown 根路径时，删除对应身份的 `.md`，保留非正文 `.clear` 水位及 `.lock`，阻止旧进程写回；即使长期记忆收集后来关闭，也可执行显式清理。

这些操作不销毁磁盘 WAL、旧备份、Phoenix 或诊断原文，不是取证级安全擦除。审计和观测有独立保留策略；清理不是全员/全产品数据库重置。

## 产品自定义扩展

底座先清默认存储，再通过产品级 ServiceKey `CONVERSATION_CLEANUP` 调用 ABC `ConversationCleanup.clear_product_memory(context)`。默认无额外存储，返回 0；旧产品不必增加方法。自定义扩展只处理额外**会话记忆**，必须按 context.identity 隔离并支持相同 idempotency_key 重试，不能删客户、考核、知识库等业务实体。

```python
from suiteharness.memory.cleanup import ConversationCleanup
from suiteharness.memory.services import CONVERSATION_CLEANUP

class MyCleanup(ConversationCleanup):
    async def clear_product_memory(self, context):
        return await self.private_chat_store.clear(
            context.identity, idempotency_key=context.idempotency_key)

# 产品激活的 bindings 返回：{CONVERSATION_CLEANUP: MyCleanup(...)}
```

扩展失败不会伪装成全部成功，返回 `failed_layers` 并提示部分失败。各数据库和文件无法共用一个事务，故底座先建立持久水位，取消旧执行，并设置清理进行中状态，再完成各层操作。重复已完成指令只重放清理回执，不清理后续新对话。

产品 A 当前没有额外独立会话存储，使用默认实现；考核批次、答案、成绩、资料和员工目录保留。产品 B 的专用 PostgreSQL 历史清理需另行绑定扩展并验证相应删除权限，本次不部署或改动 B。
