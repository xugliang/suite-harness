# SuiteHarness 中文模块指南

本文按真实目录和源码文件解释 SuiteHarness。目标是让第一次阅读代码的人知道“模块解决什么问题、如何实现、A/AB/ABC 组合时会怎样”。它不是从旧注释整理出的愿景清单；未完成部分会明确标出。

框架唯一支持的产品形态是公司服务器部署，并由公司 Web 前端和/或飞书接入；不提供、也不规划个人或桌面部署。

## 1. 先理解核心术语

| 术语 | 中文解释 | 示例 |
| --- | --- | --- |
| Harness | 驾驭与安全运行框架 | A、B 都能换工作流，但工具最终都经过同一个安全执行器 |
| Tenant | 租户，即客户公司逻辑边界 | `company-x` 的所有目录、数据和审计都带 `tenant_id` |
| Product | 产品边界 | `product-a` 与 `product-b` 可使用不同记忆和提示词 |
| Scope | 作用域，决定资源属于谁 | A 的画像绑定到 A 的 Product Scope，B 无法继承 |
| Provider | 提供器，实现某类服务契约 | A、B 可各自实现 `ProfileProvider` |
| Adapter | 适配器，把外部接口转成框架接口 | DeepSeek API 转为统一 `ModelResponse` |
| Gateway | 网关，统一入口并负责路由 | 模型网关按 A/B 选择管理员配置的模型路线 |
| Customer Bundle | 客户产品组合 | A、AB、ABC 三种购买组合 |
| Runtime Bundle | 产品内部运行能力包 | A 的知识组件依赖 A 的存储组件 |
| Plugin | 插件，经过制品信任和生命周期管理的扩展 | 部署方为 A 选择一个签名知识插件 |
| MCP | Model Context Protocol，模型上下文协议 | A 连接自己的文档 MCP，B 连接另一台服务 |
| ReAct | Reason + Act，推理并行动 | 模型提出工具意图，拿到结果后继续推理 |
| CAS | Compare-And-Swap，比较并交换 | 会话只有修订号匹配时才更新，防并发覆盖 |
| SSRF | Server-Side Request Forgery，服务端请求伪造 | 网页抓取禁止访问 `127.0.0.1` 和公司内网 |
| LIFO | Last In, First Out，后进先出 | 后安装的连接先关闭，适合回滚依赖 |
| ASGI | Asynchronous Server Gateway Interface，异步服务器网关接口 | 公司平台可直接托管 `CompanyServerApplication` |
| SSO | Single Sign-On，单点登录 | 浏览器先有公司登录态，再换 SuiteHarness 短票连接 WebSocket |

## 2. 顶层目录

```text
suiteharness/
├── .github/                CI（持续集成）、Issue 与 PR 模板
├── config/                 公司服务器配置示例
├── deploy/                 公司生产部署参考制品
├── docs/                   架构、产品开发、安全和部署文档
├── scripts/                开源发行制品检查
├── src/suiteharness/       框架源码
├── tests/                  单元、契约和安全负向测试
├── pyproject.toml          Python 项目、依赖、测试和检查配置
├── README.md               GitHub 首屏说明
├── SECURITY.md             漏洞报告政策
├── CHANGELOG.md            版本变更记录
├── CONTRIBUTING.md         贡献流程
├── CODE_OF_CONDUCT.md      社区行为准则
└── LICENSE                 MIT 开源许可
```

`.github/workflows/ci.yml` 在受支持 Python 版本上执行静态检查、测试、构建和发行内容校验；Issue/PR（问题/拉取请求）模板引导贡献者提供可复现信息。CI 通过仍不等于生产认证。

`config/suiteharness.example.yaml` 是非密钥配置样例；`config/suiteharness.secrets.example.yaml` 是密钥结构样例。实际密钥文件必须在版本库外。

`deploy/sandbox/Dockerfile` 是 Bash 沙箱最小参考镜像，安装 Bash、基础文件工具、Python 和 ripgrep，并固定运行用户 `65532:65532`；`deploy/sandbox/README.md` 说明摘要构建、工作区 ACL 和 Docker daemon 风险。它不是完整 SuiteHarness 服务镜像。

`scripts/check_distributions.py` 检查源码包和 wheel（Python 二进制发行包）是否只包含允许的框架文件，防止本地配置、业务案例、缓存或密钥进入开源制品。框架当前的交付目标仍是拉取源码构建公司部署，不是个人通过包仓库安装。

`tests/` 按源码领域分组，既测试成功路径，也大量测试越权、重放、路径逃逸、回滚失败和超限。测试通过表示当前契约行为满足断言，不等于完成生产认证。

## 3. `runtime/`：作用域、组合和生命周期内核

这是整个框架的骨架。

### `runtime/scopes.py`

定义 Root → Tenant → Product → Agent 层级。

- `ScopePath` 验证标识和父子结构；
- `RequestScope` 保存认证主体、渠道、角色、追踪标识和可选的可信持久会话所有者；
- `ServiceKey` 声明服务类型及允许在哪层绑定/覆盖；
- `ServiceBindings` 用不可变父子链查找服务；
- `RootContext`、`TenantScope`、`ProductContext`、`AgentContext` 把身份、服务和资源所有权放在一起。

AB 示例：Root 提供默认 ReAct；A 在 Product 层覆盖工作流，B 继承默认值；A/B 画像都只能各自绑定，Customer360 只能由 Tenant 拥有。

### `runtime/effects.py`

实现 `EffectScope`（副作用作用域）。连接、工具注册、异步资源和清理回调归属具体层级；关闭按子级优先、LIFO 顺序清理，并聚合失败。

例：ABC 激活到 C 时失败，C 已创建资源先清理，再清理 B、A 的本次激活资源，避免半安装组合继续服务。

### `runtime/descriptor.py`

定义严格、冻结的静态描述：

- `ProductDescriptor`：产品标识、版本、Harness API、配置模型、能力和内部能力包需求；
- `BundleManifest`：能力包标识、依赖、冲突、提供项和能力；
- `BundleRequirement` / `BundleConflict`：PEP 440 版本关系。

描述只是元数据和能力上限，不会签发工具授权。

### `runtime/bundle.py`

解析单个产品内部能力包依赖图，并按拓扑顺序安装。`BundleInstallContext` 只暴露当前产品路径、配置、服务解析和资源托管，减少安装器误碰父作用域。

例：A 的 `knowledge` 依赖 `storage`，解析结果先装 storage，再装 knowledge；两者都完成后才进入产品安装器。

### `runtime/customer_bundle.py`

处理客户购买组合：

- `CustomerProductSelection`：选择产品版本和配置；
- `Customer360SharingManifest`：默认隔离或精确策展规则；
- `CustomerBundleManifest`：完整 A/AB/ABC 声明；
- `ProductCatalog`：静态产品目录；
- `resolve_customer_bundle()`：无副作用解析为 `CustomerBundlePlan`。

产品目录只接受部署源码显式传入的 `ProductDescriptor`，不会扫描 Python 包或 Entry Point（包安装入口）并自动加载产品代码。

### `runtime/kernel.py`

`HarnessKernel` 是可信激活和执行入口：

- 校验计划与激活器完全对应；
- 调用所有产品 `prepare`；
- 检查配置、服务绑定和每产品独立画像实例；
- 解析并安装全部产品内部能力包；
- 调用产品安装器；
- 绑定 Customer360 策展策略；
- 发布 `ActivatedCustomerBundle`；
- 运行时按准确产品选择策略；
- 关闭时拒绝新运行并等待已接收运行排空。

`ProductActivationContext` 允许产品注册当前产品工具和托管资源，但不给 grant/approval（授权/审批）权限。内核同一公司只接受一个活动组合。

`runtime/__init__.py` 汇总对外公共类型；产品优先从这里导入稳定契约，不要依赖带下划线的内部符号。

## 4. `agents/`：通用 Agent 策略

### `agents/events.py`

定义 Agent 运行事件的数据结构和种类，用于观察工作流、工具和模型阶段。它是传输中立模型；当前 `ChannelApplication` 尚未把模型增量完整桥接到渠道。

### `agents/protocols.py`

定义工作流依赖的窄接口：

- `ModelGatewayLike`：只暴露模型完成请求；
- `ProductModelRouteResolver`：按产品找模型路由；
- `AgentEventSink/Stream`：事件发布/读取契约。

窄接口方便给 A/B 注入不同实现，也便于测试中用假网关替代真实厂商。

### `agents/prompts.py`

`DefaultPromptStrategy` 把服务器系统提示词、服务器重建的有界历史和当前用户输入分成不同消息，附带公司、产品、运行和历史截断元数据。共享会话中，其他认证成员的历史消息会加服务器拥有的主体标签；消息正文始终是不可信输入。它避免把用户文本直接拼进系统指令。

A 可覆盖产品提示词；B 不覆盖时继承 Root 默认。

### `agents/react.py`

`ReActWorkflow` 把历史、提示词、工具描述和观察结果转成统一模型请求。它验证：

- 恢复状态属于当前运行；
- 工具调用标识不重复；
- 模型只能选择当前执行器可见的工具；
- 模型响应和工具调用形状一致。

`ProductRoutedReActWorkflow` 由 `FoundationRuntime` 在 Root 绑定，按 A/B 产品选择模型路由。工作流只返回 `ToolIntent`，不能拿到处理器。

### `agents/reflection.py`

- `NoOpReflectionStrategy`：不做反思；
- `SinglePassModelReflectionStrategy`：用独立模型路由审查最终答案，只能接受或返回替代 JSON，且不给工具。

例：A 直接返回，C 可选择单次反思；二者的工具权限都没有变化。

## 5. `execution/`：不可绕过的工具执行边界

### `execution/models.py`

定义工具、运行、审批、审计和工作流的冻结数据模型：

- `ToolIdentity` 使用命名空间、名称、来源和版本形成安全身份；
- `ToolSpec` 声明参数 Schema、效果和所需能力；
- `ExecutionBudget` 限制迭代、调用、并行和时间；
- `ConversationContext/ConversationMessage` 是服务器重建的有界多轮上下文；
- `RunRequest/RunResult` 是统一输入输出；
- `ApprovalTarget/Binding` 绑定一次具体调用；
- `WorkflowFrame/Decision` 在工作流和执行器之间交换状态。

工具别名给模型看，完整身份给授权/审批/审计使用。

### `execution/protocols.py`

定义 `ToolRegistry`、`CapabilityAuthority`、`ApprovalStore`、`AuditJournal`、`Workflow`、`PromptStrategy`、`ReflectionStrategy` 等接口，以及三个可按产品覆盖的服务键。没有 `ExecutionRunner` 服务键，因为产品不能替换它。

### `execution/in_memory.py`

提供进程内参考实现：

- 分层工具注册表；
- 能力授权机构；
- 一次性审批存储；
- 审计列表。

适合测试和单进程验证，不适合多实例生产协调。

### `execution/sqlite.py`

`SQLiteAuditJournal` 提供追加式 SQLite 审计。公共接口没有修改/删除，事务和同步设置降低异常损坏风险；数据库文件管理员仍可直接篡改，所以它不是密码学账本。

### `execution/runner.py`

`ExecutionRunner` 是核心安全状态机。每轮：

1. 检查取消、时间和预算；
2. 渲染提示词；
3. 调用工作流；
4. 可选反思最终结果；
5. 对工具意图解析完整身份和 Schema；
6. 检查授权、只读、效果、渠道政策和审批；
7. 有界并发调用工具；
8. 把观察结果送回下一轮；
9. 记录结构化审计并返回明确状态。

远程 `$ref` / `$dynamicRef` 被拒绝，防止参数校验时访问外部 Schema。

### `security/json_schema.py`

把插件和 MCP 提供的 JSON Schema 当作不可信程序输入，统一限制节点数、嵌套深度、分支、集合及数组长度，并拒绝递归引用、正则匹配、`uniqueItems` 等可能造成网络访问或高 CPU/内存消耗的关键字。例：远端工具声明一个无限递归 `$ref` 时，会在注册前被拒绝，不会等到用户调用工具时才拖垮服务器。

## 6. `memory/`：私有画像、知识和 Customer360

### `memory/models.py`

定义角色字符串、产品私有事实、实体别名、共享规则、候选、策展决定、来源链、知识文档/命中等不可变对象。

### `memory/protocols.py`

定义四类接口：

- `ProfileProvider`：产品私有画像；
- `ProfileProviderResolver`：按产品找到私有画像；
- `EvidenceProvider`：原始证据；
- `KnowledgeProvider`：产品知识；
- `Customer360Provider`：租户级策展共享。

每个操作都显式接收 `RequestScope`，不能依赖进程全局“当前用户”。

### `memory/services.py`

把画像、证据、知识固定在 Product 层，把 Customer360 固定在 Tenant 层。服务键在绑定时就拒绝错误层级。

### `memory/in_memory.py`

包含进程内画像目录、私有事实和 Customer360 参考实现。它检查角色、公司/产品归属、幂等键、策略修订、实体修订、候选状态、双边发布/消费规则、撤回及来源链。

ABC 示例：A 候选即使审核通过，也只有规则中的 B 能消费；C 不会因为属于同一个组合自动可见。

### `memory/knowledge.py`

词法内存知识提供器先验证当前 scope 和角色，再取候选和排序，避免“先召回越权文档、后过滤”。生产向量库也应保持同样顺序。

### `memory/errors.py`

提供可机器判断的记忆错误类型和错误码，便于渠道/产品把“无权限、冲突、找不到”等情况分开处理。

## 7. `tools/` 与 `workspace/`：通用工具和文件边界

### `tools/builtins.py`

`BuiltinToolInstaller` 事务化安装九个 Root 受保护工具，并在失败时回滚：

```text
suiteharness.fs.list/read/write/edit/glob/grep
suiteharness.shell.bash
suiteharness.web.search/fetch
```

产品不能覆盖这些别名或 `suiteharness` 命名空间。

### `tools/workspace.py`

实现文件列表、读取、写入、编辑、glob（通配文件查找）和 grep（文本模式查找）。读取支持 UTF-8/GBK/GB18030，输入输出有界，grep 的正则匹配有超时，写入和编辑使用原子文件替换；没有删除工具。Linux 生产路径委托给 `workspace/secure_fs.py`，Windows pathlib 路径只作为开发回退。

每次调用先从 `MappingWorkspaceBindingResolver` 找到当前公司、产品、渠道的路径策略。A 无法用参数切到 B 的根目录。

### `tools/shell.py`

实现 Bash 工具，把脚本和工作区挂载请求交给沙箱。它声明写、破坏性和外部效果，需要工作区、进程和网络能力，生产还检查沙箱安全标志与允许出口网络。

### `tools/egress.py`

`ProductEgressArgumentPolicy`（产品出口参数策略）在认证产品已确定后校验模型选择的出口参数。Bash 只有产品列入 `sandbox.network.allowed_profiles_by_product` 才能选择相应网络；搜索/抓取未配置产品条目时只能使用公司默认 provider/route（提供器/路由），非默认值必须逐产品列出，显式空列表则连默认值也拒绝。例如 AB 可让 A 使用 `china-web` Bash 网络，而 B 保持完全断网。

### `tools/web.py`

把 `WebSearchService` 和 `WebFetchService` 包装成统一工具规格，负责参数 Schema、返回值大小和执行上下文连接。

### `workspace/layout.py`

生成并准备：

```text
tenants/{tenant}/shared
tenants/{tenant}/products/{product}
```

产品标识经过验证，根目录和子目录关系固定。

目录存在不代表产品自动获得共享权限。`workspace.shared_enabled` 默认是 `false`；启用后仍要在 `shared_access_by_product` 逐产品写 `read_only` 或 `read_write`。例如 AB 中只给 A 配 `read_write`、不给 B 配值时，A 可读写 shared，B 完全看不到 shared，两者仍各自读写自己的 product 目录。

### `workspace/access.py`

实现相对路径和可读/可写/删除策略检查。安全后端按“选定 product/shared 空间 + 规范化逻辑相对路径”授权，不把一次 `resolve` 的结果当成稍后文件打开的凭据。`WorkspaceAccessPolicy.for_feishu()` 建立飞书只读基线和可写白名单。

### `workspace/secure_fs.py`

实现 Linux 生产文件边界。它从 `/` 开始逐段以 `O_DIRECTORY|O_NOFOLLOW` 打开工作区根，再以父目录文件描述符执行最终 open/stat/scandir/replace；因此父目录在检查后被并发换成符号链接，也不会把读写转向工作区外。glob/grep 自己做有深度、条目数和时间上限的 fd 遍历，并永不进入符号链接目录。`overwrite=false` 用同目录 `linkat` 原子发布，防止检查与写入之间被抢占。

## 8. `sandbox/`：进程执行隔离

### `sandbox/models.py`

定义沙箱请求、挂载、网络模式、资源限制、结果和错误模型。所有命令用参数数组表达，避免先经过宿主 shell。

### `sandbox/protocols.py`

定义 `SandboxBackend`、底层进程传输和 `SandboxQuarantineStore`（沙箱隔离清单存储）接口，让 Docker、持久清理状态与测试替身遵循同一契约。

### `sandbox/process.py`

`AsyncioProcessTransport` 直接创建子进程、并行排空 stdout/stderr、限制保留字节，并在超时/取消时终止进程组。它是执行 Docker CLI 的传输，不等于本身提供容器隔离。

### `sandbox/docker.py`

构造受限 `docker run`：

- 生产镜像必须固定 SHA-256 摘要；
- 根文件系统只读；
- 用户固定 `65532:65532`；
- 移除能力并禁止提权；
- CPU、内存、进程、临时盘、时间和输出有界；
- 只挂载允许的产品/共享路径；
- 默认断网，出口映射只能选预配置 Docker network；
- 调用 Docker 前先持久化准确容器名的活动租约，写入失败不启动；
- 超时、取消或执行异常后只清理本次请求对应的确定容器名；
- 正常完成或确认删除后才清除租约；硬崩溃、清理不确定或清单加载失败时重启后的整个后端停止接收运行；
- `reconcile_cleanup()` 只检查和清理清单中的准确名称，确认容器消失且状态清除成功后才恢复可用。

宿主白名单工作区要让 UID/GID 65532 以受控方式访问，不能 `chmod 777`。Docker daemon/socket 是高权限边界，推荐 rootless Docker 或隔离执行节点。

### `sandbox/local.py`

本地开发后端直接在开发机执行，仅当配置是 development 且显式确认不安全时创建。它不是个人部署模式，更不是生产沙箱。

## 9. `web/`：国内搜索和安全抓取

### `web/models.py`

定义搜索查询/结果、抓取请求/响应和错误的严格模型，包括数量、字节、超时等边界。

### `web/network.py`

集中处理公开地址校验、DNS 固定、对端 IP 证明、重定向与出口策略。私网、回环、链路本地、组播、保留和未指定地址默认拒绝。

### `web/search.py`

- `BaiduQianfanSearchProvider`：调用百度千帆结构化 Web Search API；
- `MicrosoftFoundryGroundingProvider`：通过注入客户端调用 Bing grounding（搜索依据服务）；
- `SearchProviderRegistry/Service`：按配置选择 provider 并限制结果。

框架不通过解析搜索结果 HTML 伪造搜索 API。A/B 可以路由同一个搜索提供器，但仍各自经过工具授权。

### `web/fetch.py`

提供三类抓取：

- direct：直连；
- managed proxy：公司管理的 HTTP CONNECT 代理；
- browser worker：进程外浏览器工作器。

每次重定向重新做 SSRF 检查，限制压缩/解压/文本，支持常见中文编码。PDF 解析和浏览器客户端是注入接口。

## 10. `models/`：模型厂商无关层

### `models/types.py`

定义统一角色、内容块、工具、工具调用、请求、响应、流式事件、结束原因和能力标志。产品工作流只依赖这些类型，不直接绑定厂商 SDK。

### `models/protocols.py`

定义模型适配器、流式调用和路由所需接口。

### `models/registry.py`

`ProviderRegistry` 保存厂商描述和工厂；内置描述包括：

- 原生协议：OpenAI Responses、Anthropic Messages、Google Gemini、AWS Bedrock Converse、Google Vertex Gemini；
- OpenAI 兼容协议：DeepSeek、DashScope/通义千问、火山方舟/豆包、Moonshot/Kimi、MiniMax、智谱/GLM、百度/ERNIE、ModelScope、SiliconFlow、OpenRouter、AiHubMix、Groq、Mistral、StepFun、公司服务器 Ollama/vLLM。

描述中的能力是适配器声明，生产仍要按具体模型做验证。

### `models/gateway.py`

`ModelGateway` 按管理员声明的 route（路由）选择主 profile（模型配置）和后备配置，处理重试、模型覆盖白名单及流式边界。流式收到首个事件后不切换 provider，防止两家回答被拼成一个结果。

### `models/secrets.py`

通过引用解析密钥，避免产品请求直接携带 API key。

### `models/transport.py`

`HttpxTransport` 是统一 HTTP 传输适配器，不继承环境代理、不自动重定向，并对流式字节和超时做约束。需要 `httpx` 运行依赖。

### `models/errors.py`

定义模型配置、认证、限流、传输、协议和能力错误，保存可审计的 provider/profile 上下文而不泄露密钥。

### `models/adapters/_common.py`

存放多个适配器共用的 URL、鉴权、JSON/流式解析和错误映射辅助函数。

### `models/adapters/openai_responses.py`

映射 OpenAI Responses API 的输入项、内容、工具调用和 Server-Sent Events（服务器推送事件）流。

### `models/adapters/openai.py`

映射 OpenAI Chat Completions 兼容协议，是 DeepSeek、通义千问、豆包等兼容厂商的共同底层适配器。

### `models/adapters/anthropic.py`

映射 Anthropic Messages 的内容块、工具、思考/文本和事件流。

### `models/adapters/gemini.py`

映射 Google Gemini REST 协议、内容部件、工具和流式响应。

### `models/adapters/bedrock.py`

映射 AWS Bedrock Converse，通过注入的 boto3 客户端和服务器 IAM 凭据运行。`boto3` 是可选依赖。

### `models/adapters/vertex.py`

映射 Google Vertex Gemini，使用服务账号令牌提供器；不会从产品请求取得云凭据。`google-auth` 是可选依赖。

`models/adapters/__init__.py` 与 `models/__init__.py` 汇总公共适配器及类型。

## 11. `mcp/`：MCP 2025-11-25 协议栈

### `mcp/models.py`

定义 JSON-RPC（JSON 远程过程调用）消息、MCP 内容、工具、资源、提示词、补全、日志、任务、采样、引导、根目录及进度对象。`LATEST_PROTOCOL_VERSION` 为 `2025-11-25`。

### `mcp/protocols.py`

定义传输、客户端回调、`McpHttpResumeStore`（MCP HTTP 恢复存储）、可恢复传输、令牌、出口策略等接口，并把网络/沙箱具体实现留给服务器部署。

### `mcp/transports.py`

- stdio 传输要求沙箱和持久沙箱会话工厂；
- Streamable HTTP 要求 HTTPS、显式出口策略和 HTTP exchange（请求交换器）；
- 处理 session/event 标识、重连与恢复模型；
- 不信任宿主 exchange 的“已限流”承诺：核心会二次检查 stdio 实际行、HTTP 响应头/正文，以及完整和实时 SSE 的单事件大小与事件数；`max_message_bytes` 默认 4 MiB（1 KiB–64 MiB），`max_stream_events` 默认 256（1–10000），HTTP 头固定最多 128 项/64 KiB，超限失败关闭；
- 恢复模式停机使用 `close_for_resume()` 只关闭本地端，普通关闭则向已有远端会话发送 `DELETE`；
- 旧 SSE（服务器推送事件）只能通过显式兼容适配器。

### `mcp/client.py`

实现 initialize/initialized 生命周期、能力协商，以及工具、资源、提示词、补全、日志、任务、ping、取消和通知方法。只在协商能力允许时调用。

### `mcp/callbacks.py`

处理 MCP server → client 回调：roots/list、sampling/createMessage、elicitation（引导交互）、任务、日志和资源/工具/提示词变化。采样可通过统一模型网关，但不会获得额外工具权限。

### `mcp/manager.py`

按 `(tenant, product, server)` 配置、连接、查找和关闭客户端；每次操作核对 scope，避免 A 使用 B 的 MCP 连接。对启用 `resume_sessions` 的 Streamable HTTP，管理器按公司、产品、服务和配置端点加载持久游标，把远端操作在进程内串行，并在每次请求/通知之后（包括异常路径）用 CAS 保存新状态；加载、保存或修订校验失败时禁用连接。端点以配置 URL 的 SHA-256 指纹绑定，出口策略禁止把 URL 改写到另一服务。未开启恢复的服务不会访问恢复存储。

### `mcp/bridge.py`

把远程 MCP 工具注册进同一个分层工具注册表。别名确定且抗碰撞，清单同时保留 `server_id`、远端原始工具名、准确 `ToolIdentity`（工具身份）和所需能力。远端注解不具授权力；默认工具效果为写 + 外部访问，管理员覆盖必须带理由。

### `mcp/auth.py`

定义 OAuth（开放授权）服务器元数据、令牌集合、令牌提供器和缓存。它是服务器托管认证的基础契约，不是完整 OAuth 发现/发行者实现。

## 12. `plugins/`：插件信任与热生命周期

### `plugins/models.py`

定义严格 `suiteharness-plugin.json` 模型：

- 三种信任模式；
- SHA-256 摘要和签名元数据；
- 提供/依赖服务；
- product/memory/knowledge/workflow/reflection/prompt/tools/models/mcp/channels 贡献；
- 封闭权限词汇；
- 严格 JSON Schema 配置。

### `plugins/discovery.py`

只处理管理员显式路径。目录必须位于允许根，拒绝符号链接/特殊文件，对文件名、长度和内容做确定性摘要，并通过摘要允许表、签名验证器和信任策略。

### `plugins/graph.py`

解析插件服务依赖、可选依赖、版本、冲突、循环和提供项，得到确定安装顺序。

### `plugins/protocols.py`

定义插件入口、准备结果、隔离启动器、贡献导出及清理接口。

### `plugins/loader.py`

`PythonEntrypointLoader` 只用于可信进程内 Python 插件；隔离工作进程和 MCP 模式必须使用部署方注入的 launcher（启动器）返回代理。

### `plugins/registry.py`

保存已发布贡献并使用句柄令牌控制注销。旧插件句柄不能误删热替换后的新贡献。

### `plugins/lifecycle.py`

`PluginFiber` 管理单插件资源和 LIFO 清理，隔离插件失败范围。

### `plugins/manager.py`

执行“发现与校验 → 依赖规划 → 全部准备 → 校验实际导出 → 原子发布”。热替换先发布新版本再退役旧版本；失败回滚。

### `plugins/errors.py`

定义发现、制品、依赖、权限、准备、发布和回滚错误码。

## 13. `sessions/` 与 `persistence/`：状态、幂等和恢复

### `sessions/models.py`

`SessionIdentity` 精确包含公司、产品、Agent、会话和持久会话所有者；还定义会话状态、转录事件、检查点、恢复快照、原子运行 claim 状态及规范 JSON。`SessionStoreLimits` 是内存与 SQLite 共用的硬容量模型。默认所有者是当前主体；显式共享会话使用绑定公司/渠道/conversation/产品的合成所有者。

### `sessions/protocols.py`

定义创建、查询、追加、原子 claim/finalize 运行、保存检查点、改状态和恢复的 `SessionStore` 接口。

### `sessions/memory.py`

进程内会话参考实现，适合测试。它与 SQLite 使用相同配额语义：转录和运行 claim 达限即拒绝，检查点按最新窗口裁剪。

### `sessions/sqlite.py`

SQLite 实现用事务、修订号 CAS、幂等键和持久运行 claim 防并发覆盖/重复执行；恢复返回最近检查点及其后的事件。不同进程竞争同一消息时只有一个能取得执行权；租约过期后的运行标记为不确定而不是重新分配。会话/转录/claim 配额在 `BEGIN IMMEDIATE` 事务中检查，检查点按条数和累计 UTF-8 字节裁剪。

### `sessions/errors.py`

区分身份冲突、修订冲突、幂等冲突、数据损坏和超限。

### `persistence/database.py`

共享 SQLite 连接、迁移和事务基础，包含 schema metadata（结构版本元数据）、WAL 和同步策略。

### `persistence/state.py`

`SQLiteProductStateStore` 按公司/产品/命名空间/键保存有界 JSON，支持 CAS、幂等和容量限制。它是通用状态，不等同于已经实现画像或知识数据库。

### `persistence/events.py`

`SQLiteChannelEventDeduplicator` 对渠道事件做带 TTL（存活时间）的持久 claim；容量不足时失败关闭，不驱逐仍有效的 claim。

### `persistence/mcp.py`

`SQLiteMcpHttpResumeStore` 保存 MCP HTTP 会话、事件游标和端点摘要，不保存令牌。

它通过产品状态存储按 `tenant_id + product_id + server_id + endpoint SHA-256` 分区；数据库值只有 `session_id` 和 `last_event_id`，不会复制端点 URL、令牌或请求头。

### `persistence/sandbox.py`

`SQLiteSandboxQuarantineStore`（SQLite 沙箱隔离清单）按部署实例和后端保存确定的容器名称及净化后的状态。它既记录执行前的活动租约，也记录清理失败原因；生产 Docker 后端在重启后先加载这张清单，记录未由管理员核对清除前保持失败关闭。

### `persistence/models.py`

定义状态、幂等和游标对象。

### `persistence/errors.py`

提供事务、修订冲突、幂等冲突、数据损坏和容量错误。

当前 `FoundationRuntime` 自动连接会话、审计，并以同一个运行数据库创建通用产品状态、MCP 恢复和 Docker 清理隔离清单；`CompanyChannelRuntime` 应复用这个数据库连接来创建飞书持久事件去重。画像、知识和 Customer360 的生产数据存储仍由产品/部署项目提供。

## 14. `channels/`：公司入口协议和渠道政策

### `channels/models.py`

定义统一 `InboundMessage`（入站消息）和 `OutboundEvent`（出站事件），把 Web/飞书供应商格式转换成内部严格模型。

### `channels/auth.py`

定义公司身份、企业目录协议和飞书认证器。飞书外部用户标识必须映射为内部 `principal_id`，未知/禁用用户拒绝；网关对该目录调用施加配置化硬超时，异常或超时都不进入产品。

### `channels/gateway.py`

先认证再路由产品，随后构造 `RequestScope`。多产品时要求消息显式产品或唯一会话路由，不能随意选“默认 A”导致请求串产品。可注入 `ProductAccessAuthorizer`（产品访问授权器），在 Web 与飞书的统一边界按“真实公司主体 + 渠道 + conversation + 已路由产品”逐消息查询 ACL；拒绝、非严格 `True`、适配器异常或超时时不进入产品。省略时明确表示公司内全部已认证用户都能访问组合中的全部产品。

默认 `share_conversation_sessions=false`，会话 ID 和所有者都按认证用户隔离。设为 `true` 时使用合成会话所有者，成员真正共享持久历史；Web 必须逐消息调用可信 `ConversationAuthorizer` 做成员 ACL，飞书 conversation 来自已验签 chat 事件。两类 ACL 共用 `channels.authorization_timeout_seconds`（默认 10 秒、最大 60 秒）并失败关闭。共享历史不改变当前消息的工具 grant 和审批主体。

### `channels/policy.py`

`CompanyChannelAuthorizationPolicy` 评估已解析、已通过 Schema 的工具调用。Web 写/破坏性要求审批；飞书只允许白名单目录的内置写/编辑，其他写、Bash 和破坏性拒绝。

### `channels/approvals.py`

`InteractiveApprovalCoordinator` 生成挑战、等待同主体 Web 连接决定、签发一次性绑定并处理超时。协调状态在当前进程内，多实例必须替换。

### `channels/feishu/models.py`

定义飞书事件、解密后负载及发送文本结构。

### `channels/feishu/security.py`

对 Webhook 原始字节验签、解密并严格解析 JSON，拒绝重复键和非有限数。顺序很重要：不能先信任解析后的字段再验签。

### `channels/feishu/processor.py`

过滤机器人/应用消息，群聊要求提及机器人，执行事件去重并转换为内部消息。可注入持久化去重器。

### `channels/feishu/client.py`

管理飞书 tenant token 的单飞缓存和文本消息发送，避免并发重复刷新。

### `channels/feishu/long_connection.py`

定义飞书官方 SDK 长连接适配接口，SDK 负责认证、重连和 ack（消息确认）；回调处理完成前不能提前确认。

### `channels/feishu/starlette.py`

提供 Starlette（Python ASGI Web 框架）Webhook 路由适配器。它先校验唯一且合法的 `Content-Length`，再流式读取并执行正文硬上限，因此缺失或伪造长度也不能造成无界缓冲。它不是完整公司服务器进程。

### `channels/websocket/models.py`

定义客户端 message、approval decision、ping 帧和服务器 ack/event/approval/error/pong 帧，严格区分类型和大小。

### `channels/websocket/auth.py`

实现企业 SSO 后可签发的短时 HMAC 会话票据引用，并定义可替换 OIDC/JWT 认证器契约。票据不是 SSO 本身。

### `channels/websocket/server.py`

握手前核对精确 Origin 和认证；限制连接并发、帧大小和消息数；执行连接内去重；把审批挑战只投递给相同公司与主体的 socket。

### `channels/websocket/starlette.py`

把框架 WebSocket 会话接口接到 Starlette WebSocket。`Origin`、认证头、Cookie 和子协议等安全头必须各出现一次，重复头在认证前拒绝，不能借合并语义绕过校验。它是低层渠道适配器；标准 `server/asgi.py` 已把它与 SSO 登录态换短票、健康门和应用生命周期组合起来。

## 15. `config/`：只用文件的严格配置

### `config/models.py`

定义冻结且 `extra="forbid"` 的配置模型：

- 部署环境和唯一 server 模式；
- 工作区、默认关闭的共享目录及逐产品 `read_only/read_write` 权限、SQLite 路径与会话容量；
- Docker/本地开发沙箱；
- 顶层服务器监听/并发/启停超时，Web/飞书路径及产品路由；
- 模型 profile/route；
- 百度/Foundry 搜索和三类抓取；
- 产品级 MCP；
- 插件路径、摘要和信任；
- 单独密钥结构。

跨字段验证会拒绝生产本地沙箱、浮动 Docker 镜像、Web 非 HTTPS Origin、WebSocket/会话交换/Webhook/健康路由冲突、多产品飞书无路由、未知产品/模型/MCP/出口/共享工作区引用、未定义的出口名称、关闭共享却声明共享权限、飞书共享写却没有产品级 `read_write` 等组合。至少启用一个公司渠道。`server.host/port` 供 ASGI 监听；Web `session_lifetime_seconds` 限制为 30–900 秒；公司认证和渠道 ACL 超时都有 60 秒硬上限且拒绝字符串/布尔值伪装成数字。

### `config/loader.py`

`SuiteHarnessConfigLoader` 读取两条显式 YAML 路径，限制 1 MiB、拒绝符号链接、使用 UTF-8、拒绝 YAML 别名/非标准类型、校验密钥文件权限、解析引用并脱敏错误。它不读取环境变量覆盖。

### `config/__init__.py`

汇总部署项目需要的配置类型。

## 16. `server/`：基础设施装配与渠道应用主链

### `server/foundation.py`

`FoundationRuntime` 根据已验证配置创建共享底座：

- 工作区和渠道绑定解析器；
- Docker 或本地开发沙箱和逐产品 Bash/搜索/抓取出口参数；
- 模型注册表、网关和路由；
- 搜索/抓取；
- 带硬容量配置的 SQLite 会话、审计，以及运行数据库中的产品状态、MCP HTTP 恢复和沙箱清理隔离清单；
- 进程内工具、授权和审批；
- Root 默认 ReAct/提示词/反思；
- HarnessKernel 和渠道安全策略；
- 九个受保护内置工具；
- 插件/MCP 扩展宿主。

`await FoundationRuntime.create(...)` 是公司入口应使用的异步工厂：它完成组装后探测沙箱、运行数据库、会话和审计，任何部分构造或启动失败都会反向回滚。`readiness()` 还报告插件与 MCP 状态。`prepare_product_workspace()` 只接受客户组合已选择的产品，为 A/B/C 创建目录并注册 Web/internal/飞书不同策略：product 根默认可读写，shared 默认不可见并按产品显式放开。`close()` 按扩展、内核、工具、存储和模型顺序聚合清理，最后关闭 Foundation 拥有的运行数据库。

Foundation 自身不是完整应用：它不会选择产品激活器、创建渠道授权模板或连接企业认证；这些由 `server/asgi.py` 在更高一层组合。

### `server/access.py`

`ToolAccessTemplate` 按准确 `(channel_id, product_id)` 分类普通内置/产品工具的只读、写和破坏性别名。`ConfiguredGrantIssuer` 运行前把别名解析为完整身份和能力，签发绑定主体的短时 grant，运行结束撤销。

MCP 不使用模板中的布尔开关。`mcp.channel_access.web|feishu.<product>` 是唯一白名单来源：`allow_servers` 选择某服务当前发现的全部工具，`allow_tools` 按服务和远端原始工具名精确选择，默认空即拒绝。扩展宿主提供的 `AdditionalGrantMaterial`（附加授权材料）保存已发现工具的服务、原始名、准确身份和能力；租约签发时再次对照注册表，身份或能力漂移就失败。飞书模板只能把内置 `suiteharness.fs.write/edit` 放入写集合，不能配置破坏性普通工具；飞书选中的 MCP 也必须实际为只读。路径白名单由另一层渠道/工作区策略检查。

### `server/application.py`

`ChannelApplication` 串起：

```text
认证消息 + RequestScope
→ 会话输入
→ started 检查点
→ 短时工具授权 lease（租约）
→ ActivatedCustomerBundle.run
→ 终态转录/检查点
→ started/completed/failed 事件
```

稳定运行标识和消息摘要用于重投；不确定副作用不会自动重跑。进程内会话锁保证顺序，SQLite CAS 防止多进程同时推进同一会话。

每轮还会从可信转录中重建 `ConversationContext`：只保留成功配对的 user/assistant，清除 metadata、附件、失败/取消和未配对轮次，并限制扫描事件、消息数和总字节。默认配置最多扫描 256 个事件、最多 32 条上下文消息、最多 128 KiB；超限会截断并向提示词策略标记。共享会话送入模型的提示词窗口用 `participant-1` 等临时别名代表其他成员，不暴露其真实 `principal_id`。

### `server/extensions.py`

`ServerExtensionHost` 分两阶段：

1. 发现、验证和激活显式插件；
2. 客户产品激活后，按产品连接 MCP 并把工具注册到对应 Product Scope。

启动失败事务化回滚，关闭时先 MCP 后插件。Foundation 会把 MCP 恢复存储注入扩展宿主；若某个 Streamable HTTP 服务要求恢复而适配器不支持可恢复传输，或使用旧 SSE 兼容模式，启动会拒绝。Foundry、浏览器、插件隔离、MCP HTTP/stdio 等外部系统通过 HostAdapters（宿主适配器）注入。

### `server/host.py`

提供真正的企业渠道组合器：

- `WebApprovalRuntime` 把 WebSocket 审批 hub 和执行器 coordinator 组成同一套审批回路，必须在 Foundation 之前创建；
- `FeishuHostAdapters` 要求公司目录、出站发送器，以及按传输选择的 Webhook 解密器或官方长连接 SDK；
- `CompanyChannelRuntime.create()` 校验 activation 与配置完全一致，要求普通工具模板准确覆盖所有启用渠道 × 产品，并按 `mcp.channel_access` 从已发现 MCP 清单选工具；配置的精确工具未发现、租约时身份/能力漂移或飞书选中非只读 MCP 都失败关闭；它还创建 `ChannelApplication`、WebSocket、飞书网关和持久飞书去重，标准装配传入 `foundation.runtime_database`，共享 Web 会话时强制要求可信 `ConversationAuthorizer`；
- `starlette_routes()` 返回启用的 WebSocket/Webhook 路由；
- `run_feishu_long_connection()` 运行官方 SDK 适配器；
- `close()` 停止长连接、审批，并只关闭由它内部创建的运行数据库；外部注入数据库仍归调用者。

`server/host.py` 仍是低层组合器；标准启动顺序和进程入口由下一节的 `server/asgi.py` 拥有。

### `server/asgi.py`

提供公司服务器的标准闭环：

- `CompanyServerBootstrap.from_files()` 从两份显式 YAML 加载配置，并要求部署方注入产品目录、激活器、授权模板及启用功能需要的可信适配器；
- `build()` 依次解析组合、创建唯一 Web 审批回路、调用 `FoundationRuntime.create()`、准备工作区、激活产品、启动插件/MCP、创建渠道和启动可选飞书长连接；任一步失败都反向回滚；
- `CompanyServerApplication` 固定提供 `/health/live` 和 `/health/ready`，Web 启用时再提供默认 `POST /auth/session`；只有 lifespan 成功且全部就绪后才动态加入 WebSocket/飞书 Webhook 路由；
- `CompanyHttpAuthenticator` 验证浏览器已有的公司 Cookie/Bearer（持有者）登录态，框架核对 tenant 后换发 30–900 秒的 HMAC Bearer WebSocket 票据。请求正文不被接受，Origin 和请求头有严格边界；真实 SSO、Cookie 场景的 CSRF、账号状态和角色同步仍由公司适配器负责；
- `create_company_asgi_app()` 返回可调用的 ASGI 应用；`run()` / `serve()` 使用顶层 `server` 监听配置运行 Uvicorn，关闭时先渠道、再飞书后台任务、最后 Foundation。

健康门只公开 `ok/failed`。liveness（存活）代表事件循环可响应；readiness（就绪）覆盖已启动底座、存储、沙箱、插件/MCP、产品、渠道和可选长连接，但不深度探测模型厂商、公司 SSO、搜索/抓取或飞书出站 API。仓库没有预制全局 `app`、命令行脚本或完整公司服务镜像；公司部署项目仍要提供可信适配器、反向代理、监控和编排。

### `server/__init__.py`

导出部署项目需要的 Foundation、ChannelApplication、授权模板、企业渠道组合器、公司 ASGI 应用和扩展接口。

## 17. 三条跨模块主线

### A 单产品

```text
配置选择 A
→ A 描述解析
→ Foundation 准备 A 工作区
→ A Activator 绑定画像/策略
→ A MCP 和产品工具进入 A scope
→ Web/飞书请求路由 A
→ 统一 Runner 执行并写 A 会话/审计
```

### AB 多产品

```text
配置选择 A + B
→ 两者全部 prepare
→ 两者内部 bundle 全部安装
→ A/B 产品安装器才运行
→ A/B 各自画像、工作流、模型路由、工具和 MCP
→ 默认互不可见
→ 只有 curated 精确事实可跨产品
```

### 公司用户一次 Web 写入

```text
SSO 身份 → WebSocket 认证 → 产品路由
→ (web, product-a) 模板签发短时精确授权
→ 模型提出 fs.edit
→ Runner 校验参数/身份/效果
→ Web 交互审批绑定参数摘要
→ 工作区路径检查
→ 原子编辑
→ 审计 + 会话终态
```

飞书同一请求会在审批之前按渠道策略拒绝，除非管理员同时开放内置 edit 和目标白名单目录；即使开放，也绝不允许删除。

## 18. 哪些是接口，哪些是可直接参考的实现

| 能力 | 当前形态 |
| --- | --- |
| 客户组合、作用域、执行器、内置工具、Docker 后端 | 已实现框架逻辑 |
| 公司 ASGI、WebSocket/飞书与 Starlette 适配 | 已实现标准组合器、健康门和生命周期，仍需公司可信适配器及生产托管 |
| SQLite 会话/审计/状态/去重/恢复 | 单服务器参考实现 |
| 画像/Customer360/词法知识 | 进程内参考实现 |
| Evidence | 协议，无通用生产 provider |
| SSO/IAM、飞书公司目录 | 接口，由部署方实现 |
| Foundry 客户端、浏览器工作器 | 接口，由部署方实现 |
| 插件签名、隔离工作进程 | 接口，由部署方实现 |
| MCP 网络 exchange、出口、stdio 会话 | 接口，由部署方实现 |
| 高可用、分布式限流/审批/授权 | 未提供 |
| 一键生产服务与编排 | 未提供 |

因此，“模块存在”不能自动理解为“外部基础设施已经可用”。公司部署项目应从 [部署指南](deployment.zh-CN.md) 的验收清单逐项补齐。
