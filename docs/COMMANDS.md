# 可选产品命令路由

框架只提供命令机制；具体命令词、业务、角色、确认状态和数据表属于产品。
未提供 `command_registry` 时，所有旧工作流和产品保持原路径，不要求实现新的抽象方法。

可信部署为一个 Kernel 创建一个 `InMemoryCommandRegistry`，按完整产品作用域调用
`register(scope, specs, handler=..., command_only_tools=...)`，传入
`CompanyServerBootstrap(command_registry=registry)`。默认 `UniqueCommandHandler` 可处理唯一匹配；
产品继承 `CommandHandler` ABC 处理冲突和语法，返回注册过的命令 ID 或无命令。
重复注册和别名冲突会拒绝；可停止该产品新请求后 `unregister(scope)`，
或在整个专属实例停止使用后 `clear()`。不要在运行中把同一注册器共享给不同部署。

`CommandSpec` 支持 `contains`、`prefix`、`exact`，以及前缀边界控制。
注册为空别名的内部路由，可让产品把多个冲突命令统一导向澄清。
命中的命令不能由处理器返回空值退回不受限模式。

执行器在调用策略之前，从本次真实输入创建不可变 `CommandContext`，绑定完整请求作用域、
运行 ID 和原文摘要。上下文不读取历史消息或 metadata；字符串及已有频道 message/text
封装可用，敏感前缀前的空白不会被自动剥除。工作流可读取 `frame.command_context`，
但模型篡改 frame 不会改变执行器保存的权限约束。

命令限定的工具名在请求开始时绑定到真实注册工具身份，在工具展示和 handler 执行之前
分别收窄；仍需通过原有 grant、capability、只读策略和输入校验。命令不会额外授权。
`command_only_tools` 允许禁止某些工具走无命令入口，但不要求所有写工具必须带命令。
未注册的其他产品保持原行为。

业务服务仍须核实用户、目标对象、预览确认、权限和幂等状态；这些不能仅依赖提示词或路由。
命令上下文不是业务审批凭据。恢复任务时仍根据当前授权重新执行检查。

审计区分工具 handler 内部异常（`tool_error`）和返回值序列化异常（`invalid_tool_result`），
附带错误阶段和异常类型，不输出异常正文或敏感参数。`run_finished=succeeded` 只表示
对话执行完成，不代表业务副作用成功；产品应根据持久化业务状态和实际发送回执展示结果。

回归入口：`tests/test_commands.py`。覆盖匹配、别名、产品隔离、卸载、伪造上下文、工具身份
替换、绕过工具展示、参数错误、错误分类及 Bootstrap / Foundation / Kernel 传递链。
