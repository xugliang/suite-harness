# 上下文字符统计

这项能力用于定位哪些输入占用较大，不是 tokenizer（模型分词器），也不修改
上下文预算、压缩策略或供应商返回的真实 token 用量。无需新增数据库，沿用
`suiteharness.diagnostics` 的安全 JSON 日志、单请求导出和文件轮转配置。

## 统计口径

使用 Python `len(str)` 统计 Unicode 码点。一个汉字、普通标点或换行各计 1；
组合字符和组合表情可能由多个码点组成。不是 UTF-8 字节数，也不是字形数。

| 分类字段 | 含义 |
| --- | --- |
| `system_prompt_chars` | 系统规则及服务器运行环境 |
| `history_memory_chars` | 历史对话、工作摘要和长期记忆 |
| `tool_definitions_chars` | 工具名称、说明和输入 JSON Schema |
| `current_execution_chars` | 本次执行产生的助手内容、工具名称、调用参数和返回结果 |
| `current_question_chars` | 当前用户问题 |
| `unclassified_chars` | 未标注、无法可靠判断来源的内容及最终回答 Schema |

消息文本原样计算，工具 Schema 和调用参数按 `ensure_ascii=False` 的紧凑 JSON
计算（包含 JSON 标点）。非文本图片/文档计入 `nontext_parts`，其 URL 和 base64
不折算正文字符。供应商消息封装、调用关联 ID、路由元数据、采样参数和 stop
控制项不计入正文。`total_chars` 始终等于六个分类之和。

## 日志事件

- `model.started`：请求收到时的 `before_*`，尚无压缩后结果。
  输出上限使用 `max_output_tokens`，不与实际用量 `output_tokens` 混用。
- `model.context_ready`：指定模型配置的压缩前 `before_*`、压缩后 `after_*`、
  差值 `reduced_*`，以及已有预算估计、删除轮数、截断工具数。
- `model.context_failed`：本地校验或容量不足。在容量不足时，也记录最终尝试
  压缩后的大小；其他无法取得压缩后结果的错误使用 `after_known=false`，不伪造 0。
- `model.attempt_started`：每次适配器调用均携带同一组前后计数及 `attempt`。
  重试按真实调用次数记录，不能把多次重复输入相加解释成单次窗口大小。
- `model.attempt_finished`：该次调用的成功、失败或取消状态，以及实际取得的
  供应商 `input_tokens` / `output_tokens`。未取得用量时不造数。

`provider_invoked=true` 表示进入供应商适配器调用，不意味着远端已收到、计费或
成功。尚在本地上下文准备阶段为 false。`streaming` 区分流式与非流式。
同一次逻辑调用使用 `model_call_id` 关联；回退模型通过 `profile_id` 区分。

差值是有符号的：`reduced_total_chars = before_total_chars - after_total_chars`。
极短文本转换成截断 JSON 包装时，个别分类差值可能为负，不隐藏这部分增长。
日志只包含固定分类的数字和安全状态字段，不输出消息正文、工具参数、密钥、
图片编码或异常消息。单请求导出仍使用 `python -m suiteharness.diagnostics`。

## 产品如何标注

底座默认提示词策略会标注历史/记忆、系统提示词和当前问题；ReAct 会标注本次
助手消息及工具结果。产品自定义提示词时，建议在创建位置提供分类：

```python
PromptMessage(role="user", content=summary, context_category="history_memory")
ModelMessage.text(ModelRole.USER, question, context_category="current_question")
```

分类仅供诊断，不改变消息角色或权限。未标注的系统消息归入系统提示词，其他
未标注消息保留为 `unclassified`，不会按内容猜测来源。此字段不发送给供应商，
通用序列化也不包含它。ReAct 检查点通过可选的顶层 `context_categories` 数组
保留分类，旧检查点无此字段仍可恢复，旧版本读取器可忽略这个顶层字段。

如果产品另有自定义检查点，应自行保留该内部分类；丢失时只会导致未分类增加，
不会改变问答或记忆内容。
