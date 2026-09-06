# Changelog

本项目按 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 记录用户可见变化，版本采用 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)（语义化版本）。

`0.x` 期间公共接口仍可能调整；升级前请阅读变更并运行产品契约测试。

## [Unreleased]

### Added

- WebSocket 使用限定路径的 HttpOnly/Secure/SameSite Cookie，并在每帧前及空闲周期内重新验证短票和账号状态；撤销时取消该连接的在途运行；
- 通道附件只通过服务端资源 ID、产品/租户/主体授权和二次存储归属校验解析，支持文本与图片模型内容；
- 模型 profile 可声明具体模型的流式、工具、并行调用、JSON Schema、推理、视觉、文档和上下文能力差异，扩大厂商默认能力须显式确认；
- 飞书事件处理租约、总处理超时、完成态去重和旧私聊历史兼容的可信会话作用域；
- Docker context 与 rootless daemon 强制探测，生产就绪同时核验固定摘要镜像已存在；
- Root 服务绑定向可信产品 activator 暴露统一模型网关，产品仍只能使用管理员声明的 route/profile/credential。

### Changed

- Starlette 最低版本提升到 `1.3.1`，避开 2026 年已公开且只在 1.x 修复的安全问题；目标生产环境仍须生成带哈希锁文件并执行漏洞审计。

### Security

- 配置和 secrets 通过单一文件描述符读取，拒绝链接、非普通文件、身份复用、宽松 POSIX 权限、超限正文和生产占位值；
- WebSocket 票据到期、账号停用和角色变化在空闲连接上也会有界生效；
- 飞书崩溃前的处理中事件仅保留短租约，成功后才转为长期去重标记，避免永久丢失或 ABA 删除；
- 沙箱镜像和 context 拒绝命令选项样式值，rootless 要求无法验证时失败关闭。

### Planned

- 公司服务镜像、编排模板、命令行入口和可运行参考组合项目；
- 企业 SSO/IAM、飞书目录及外部服务的参考宿主适配器；
- 生产画像、知识和 Customer360 持久化适配示例；
- 多实例授权、审批、限流和协调接口的生产实现。

这些项目是规划方向，不代表已承诺版本或时间。

## [0.1.0] - 2026-09-05

### Added

- Root → Tenant → Product → Agent/Session 作用域、不可变服务绑定和 LIFO 资源所有权；
- 严格 `ProductDescriptor`、产品内部 Runtime Bundle 依赖图，以及 A/AB/ABC `CustomerBundleManifest` 解析；
- 两阶段产品准备/安装、跨产品安装屏障、完整失败回滚和运行排空；
- 默认按 `tenant_id + product_id` 隔离的画像契约；
- 显式 `producer → consumer → predicate` 策展式 Customer360、实体映射、候选审核、来源链和撤回；
- 产品级画像、证据、知识、工作流、提示词和反思扩展接口；
- Root 拥有的 `ExecutionRunner`，包含精确工具身份、短时授权、JSON Schema、渠道策略、一次性审批、预算、取消和审计；
- 九个受保护内置工具：文件 list/read/write/edit/glob/grep、Bash、Web Search、Web Fetch；
- 公司/产品工作区布局、默认关闭且逐产品只读/读写的共享目录，以及 Web/internal 与飞书的独立路径策略；
- Docker 生产沙箱、本地开发后端、有界进程传输，以及执行前活动租约、硬崩溃/清理失败后的持久隔离、失败关闭和准确名称核对恢复；
- 百度千帆结构化搜索、Microsoft Foundry Bing 适配接口，以及直连/公司代理/浏览器工作器抓取；
- Bash、Web Search 和 Web Fetch 的逐产品出口参数策略，未知产品/出口配置失败关闭；
- OpenAI Responses、Anthropic Messages、Google Gemini、AWS Bedrock、Google Vertex 及多个 OpenAI 兼容厂商的统一模型适配层；
- 模型主/后备路由、重试、能力声明和不可拼接的流式故障边界；
- MCP `2025-11-25` 客户端协议面、stdio/Streamable HTTP 传输、回调、任务、OAuth 模型、产品级工具桥，以及按公司/产品/服务/端点绑定的可选持久恢复；
- 严格插件清单、摘要/签名接口、权限上限、依赖图、两阶段生命周期、原子发布和热替换；
- SQLite 会话、检查点、审计、产品状态、渠道去重、MCP 恢复游标和 Docker 清理隔离清单；
- 会话存储硬容量配置、转录/运行 claim 达限失败关闭、检查点条数与累计字节窗口；
- WebSocket 企业票据、Origin 白名单、严格帧、交互审批和 Starlette 适配；
- 默认用户隔离的持久多轮历史，以及显式共享会话的合成所有者、Web 成员 ACL 接口、有界可信历史重建和对其他参与者真实主体标识的提示词别名化；
- Web/飞书共用的可选逐消息产品 ACL 接口，在产品路由后失败关闭；
- 产品 ACL 与共享 Web 会话 ACL 的统一可配置超时（默认 10 秒、最大 60 秒）；
- 飞书 Webhook/长连接、原始请求验签、企业目录认证、事件过滤/去重和文本出站适配；
- `FoundationRuntime` 基础设施装配与统一运行数据库、`ChannelApplication` 持久运行主链和 `CompanyChannelRuntime` 企业渠道组合器；
- 可回滚的 `FoundationRuntime.create/start/readiness`，以及标准公司 ASGI 组合器、SSO 登录态换短票、健康/就绪门、应用生命周期和 Uvicorn 入口；
- 按 `(channel, product)` 精确覆盖的普通工具模板，以及默认全拒绝的 `mcp.channel_access` 产品/服务/远端工具白名单、启动发现校验和租约时身份/能力复核；
- 飞书全局只读、白名单目录内仅 write/edit、永不删除的双层策略；
- 严格公开/密钥 YAML 配置加载，生产安全组合校验；
- Bash 沙箱参考 Dockerfile 和中文架构、安全、产品开发、模块、部署文档。

### Security

- Linux 文件工具改为根目录 fd 锚定、逐段 `O_NOFOLLOW` 的无链接遍历；原子写入使用同父目录 fd，且 `overwrite=false` 具备无覆盖竞态语义；生产缺少该后端时失败关闭；
- 工具授权、审批、取消和审计绑定完整作用域、主体、运行、调用、工具身份与参数摘要；
- 内置命名空间防覆盖，远程 JSON Schema 引用拒绝；
- 文件真实路径/符号链接边界、原子写入和飞书删除拒绝；
- Docker 使用只读根、固定 `65532:65532` 用户、移除能力、禁止提权、资源上限和默认断网；
- Docker 清理结果不确定时持久隔离整个后端，重启不自动清除；
- Web 抓取在每次重定向执行 SSRF 地址检查、DNS 固定和对端 IP 证明；
- MCP 远端工具默认按写 + 外部访问处理，远端注解不具授权力；
- MCP 核心对 stdio 行、HTTP 头/正文及完整/实时 SSE 的消息大小和事件数二次设限，超限失败关闭；
- MCP 恢复游标每次操作后以 CAS 持久化，存储失败禁用连接，且不保存令牌、认证头或端点 URL；
- 插件只从显式路径发现，摘要、签名、权限和信任模式在导入前验证；
- 渠道消息重投可重放终态，崩溃后的不确定副作用失败关闭；
- 跨进程原子运行 claim 与所有者围栏，过期运行永久按不确定状态处理而不自动重执行；
- 飞书 Webhook 流式正文硬上限，以及 WebSocket 重复安全头的认证前拒绝；
- 不可信插件/MCP JSON Schema 的节点、深度、分支、集合和数组预算，以及高风险关键字拒绝；
- Web SSO 正文真实流式读取、认证调用硬超时、固定 1 MiB WebSocket 协议帧，以及飞书事件/沙箱租约的令牌化 compare-delete（比较后删除）防 ABA 竞态。

### Known limitations

- Alpha 阶段尚无预配置全局 ASGI `app`、命令行脚本、完整公司服务镜像和编排模板；框架已有需注入可信适配器的标准 ASGI 应用；
- 企业 SSO、飞书目录、Foundry、浏览器工作器、插件隔离/签名和 MCP 宿主端口需要部署方实现；
- 画像、Customer360、词法知识、能力授权、审批和工具注册主要是进程内参考实现；
- SQLite 参考存储不构成多实例高可用方案；
- 原始证据只有协议，没有通用生产 provider；
- 当前渠道应用主链输出 started/completed/failed，尚未转发完整模型增量；
- 生命周期回调没有内核强制超时，同进程可信扩展不受 Docker 沙箱隔离；
- 适配器存在不等于所有厂商模型/区域/版本已完成生产契约认证。
