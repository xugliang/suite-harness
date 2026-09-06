# SuiteHarness 公司部署指南

SuiteHarness 永远只支持公司服务器 + 公司级 Web 前端和/或飞书，不提供个人、桌面、单用户常驻助手模式。

本文针对 `0.1.0` Alpha。仓库已有标准公司 ASGI（异步服务器网关接口）组合器、SSO（单点登录）凭据换短票路由、健康门、lifespan（应用启停生命周期）、Uvicorn 便捷入口和 Bash 沙箱参考镜像；但没有预制全局 `app`、命令行脚本、真实公司 SSO、产品实现、完整服务镜像或一键编排。部署项目必须完成本文列出的可信适配和生产托管。

## 1. 支持的拓扑

```text
                   ┌─ 企业 SSO / IAM
公司 Web 前端 ────┤
  WebSocket        └─ 短时公司会话票据
                         │
                         ▼
                  SuiteHarness 公司服务器
飞书企业应用 ───────►  一个客户公司一套部署
 Webhook/长连接           │
                ┌────────┼───────────┐
                ▼        ▼           ▼
             模型服务   MCP 服务   Docker 沙箱执行节点
                │
                ▼
         公司数据库/工作区/审计
```

推荐每个客户公司独立：

- 服务进程或编排命名空间；
- 公开配置和密钥；
- SQLite/生产数据库；
- 工作区根；
- Docker 出口网络；
- 插件信任清单；
- 日志、审计和备份。

代码保留 `tenant_id`（租户标识）是为了形成纵深防御和可迁移的数据键，不表示当前推荐多个客户共用一套进程。

## 2. 运行环境

### 2.1 开发环境

- Windows 或 Linux；
- Python 3.11+；
- 可使用本地开发沙箱，但必须是 `environment=development`、`backend=local`、`acknowledge_unsafe=true`；
- 本地后端会直接执行命令，不具备安全隔离。

开发环境仍是公司源码开发，不是个人部署产品形态。

### 2.2 生产环境基线

- Linux 服务器；这是当前唯一受支持的生产基线，启动时会验证 `dir_fd`、`O_NOFOLLOW` 和 fd 目录遍历能力，不具备则失败关闭；
- Python 3.11+；
- Docker，或由部署方实现等价且通过验收的 `SandboxBackend`；
- 摘要固定的沙箱镜像；
- 公司管理的 HTTPS、证书、反向代理、SSO/IAM、密钥和日志系统；
- 至少启用 Web 或飞书之一。

生产配置会拒绝本地开发沙箱、浮动镜像、Web 非 HTTPS Origin（网页来源）和部分不完整的多产品路由。

Windows 只用于源码开发。其 pathlib 文件回退实现不构成抵抗并发符号链接替换的安全边界，不能通过配置把它提升为生产后端。

## 3. 从源码构建，不等于无需依赖

本项目当前采用“拉取源码 → 公司受控环境安装依赖 → 构建公司服务制品”，不提供面向个人的包仓库安装或桌面启动方式。

`pyproject.toml` 中的核心运行依赖包括：

- Pydantic：配置和协议数据校验；
- jsonschema：工具参数 JSON Schema 校验；
- packaging：PEP 440 版本解析；
- httpx：模型及受控 HTTP 传输；
- PyYAML：配置文件解析；
- Starlette：ASGI（异步服务器网关接口）路由适配；
- Uvicorn：ASGI 服务运行器；
- regex：为 grep 正则匹配提供可中止的超时边界。

可选模型依赖：

- `aws`：boto3，用于 AWS Bedrock；
- `google`：google-auth，用于 Google Vertex；
- `all-model-providers`：同时安装上述两组。

公司应通过内部镜像、锁文件和制品扫描安装 `pyproject.toml` 声明的依赖，并固定实际解析版本。仅把源码复制到服务器而不安装依赖，服务无法运行。

开发质量检查：

```bash
python -m ruff check .
python -m pytest
python -m build
python scripts/check_distributions.py dist
```

当前仓库不替部署项目生成完整依赖锁文件；上线项目应自行锁定、生成 SBOM（软件物料清单）并做漏洞扫描。

## 4. 服务器目录和权限

建议：

```text
/opt/suiteharness/                       只读框架与公司宿主源码
/opt/suiteharness/plugins/               只读、签名插件制品
/etc/suiteharness/suiteharness.yaml              公开配置
/etc/suiteharness/suiteharness.secrets.yaml      密钥，仅服务账号可读
/var/lib/suiteharness/state/             会话、审计和运行状态
/var/lib/suiteharness/workspaces/
  tenants/{tenant}/shared/
  tenants/{tenant}/products/{product}/
```

要求：

- 框架、宿主源码和插件由部署账号写、运行账号只读；
- Linux 上密钥文件应 `chmod 600`，不得是符号链接；配置加载器使用不跟随符号链接的
  文件句柄读取，再以 `fstat`（已打开文件状态）核对普通文件、权限、身份和 1 MiB 上限，
  避免“先检查路径、再被替换”的竞态；
- 插件 `allowed_roots` 及其每一级祖先必须由 root/部署管理员拥有，SuiteHarness 服务账号和插件代码不得写入，并在容器内只读挂载；摘要复核会缩小但不能彻底消除 Python 路径导入竞态；
- 状态目录只给服务账号；
- 沙箱挂载的产品/共享工作区只包含 Agent 允许处理的数据；
- 不挂载 Docker socket、宿主密钥、`/etc`、`/var/run` 或整个项目源码；
- 不用 `chmod 777` 解决容器写权限。

## 5. Docker Bash 沙箱

仓库提供 [参考 Dockerfile](../deploy/sandbox/Dockerfile) 和 [镜像说明](../deploy/sandbox/README.md)。

### 5.1 构建

基础镜像必须先固定真实摘要：

```bash
docker build \
  --build-arg BASE_IMAGE='debian:bookworm-slim@sha256:<真实摘要>' \
  --tag registry.example.com/suiteharness/sandbox:0.1.0 \
  deploy/sandbox

docker push registry.example.com/suiteharness/sandbox:0.1.0
docker inspect --format='{{index .RepoDigests 0}}' \
  registry.example.com/suiteharness/sandbox:0.1.0
```

把最终仓库摘要而不是标签填入：

```yaml
sandbox:
  backend: docker
  required: true
  context: suiteharness-rootless
  require_rootless: true
  image: registry.example.com/suiteharness/sandbox@sha256:<64位真实摘要>
```

生产服务账号必须在配置的同一个 Docker context 中预先拉取该摘要镜像：

```bash
docker --context suiteharness-rootless pull \
  registry.example.com/suiteharness/sandbox@sha256:<64位真实摘要>
docker --context suiteharness-rootless image inspect \
  registry.example.com/suiteharness/sandbox@sha256:<64位真实摘要>
```

readiness 会同时核验 daemon、`require_rootless`（若启用）和本地精确摘要镜像；不会在
处理请求时临时拉取一个尚未验收的镜像。镜像缺失、context 指错或 daemon 不是 rootless
都会使服务保持 not-ready。

### 5.2 运行约束

`DockerSandboxBackend` 生产运行固定：

- `--user=65532:65532`；
- `--read-only`；
- `--cap-drop=ALL`；
- `--security-opt=no-new-privileges`；
- CPU、内存、PID（进程标识数量）、tmpfs（内存临时盘）、时间和输出限制；
- 默认 `--network=none`；
- 只挂载当前请求允许的工作区。

宿主的 product/shared 工作区必须以受控方式允许 UID/GID `65532:65532` 读写：

- 可对准确产品目录设置属主/属组；
- 或使用文件系统 ACL（访问控制列表）只授权准确目录；
- 飞书可写子目录还要继续受渠道白名单限制；
- **不能**对工作区、父目录或状态目录执行 `chmod 777`。

### 5.3 Docker daemon 是高权限边界

能访问 Docker daemon/socket 的进程通常可获得近似宿主 root 的能力。必须：

- 不把 `/var/run/docker.sock` 挂给产品、插件或沙箱容器；
- 限制 SuiteHarness 服务账号与 Docker API 的关系；
- 优先使用 rootless Docker；
- 或把沙箱放到独立执行节点，由窄化服务接口接收任务；
- 监控异常容器、镜像拉取和网络变化；
- 定期更新宿主内核、Docker 与基础镜像。

`sandbox.context`（Docker 上下文）会以 `docker --context <name> ...` 的形式应用到可用性
探测、运行、超时清理和隔离恢复的每一条命令。生产建议为 SuiteHarness 服务账号创建
固定名称的 rootless context 并在配置中显式填写；这样不会因为 shell 当前环境或默认
context 改变而误连 rootful daemon。context 名只允许字母、数字、点、下划线和连字符，
不能注入 `--host` 等额外 CLI 参数。仅填写 context 名并不会自动把对应 daemon 变安全，
上线仍须核验其 endpoint 确实属于目标非特权账号。启用 `require_rootless: true` 后，readiness
还会通过 `docker info` 检查 daemon 的 `name=rootless` 安全标志；无法解析、没有该标志或
daemon 不可用都失败关闭，且不会执行任何沙箱命令。

Docker 沙箱主要保护 Bash 和可接入的 stdio MCP；它不会自动隔离可信进程内 Python 产品/插件。

### 5.4 清理失败隔离与运维恢复

生产 `FoundationRuntime` 会在运行数据库中创建持久 `SQLiteSandboxQuarantineStore`（SQLite 沙箱隔离清单）。每次调用 Docker 前，后端先为本次请求派生出的 `suiteharness-sandbox-<摘要>` 确定名称写入 active lease（活动租约）；写入失败就不启动容器。正常完成或准确名称强制删除成功后才清除租约。进程硬崩溃、运行超时、取消或 CLI 异常时，该记录仍可阻止重启后继续执行。若删除命令超时、失败或抛错：

1. 保留已有活动租约，并尝试更新准确容器名对应的净化失败原因；
2. 整个 Docker 后端停止接受新运行；
3. 服务重启后仍加载隔离状态，不会因重启自动放行；
4. 管理员调查后调用 `await foundation.sandbox.reconcile_cleanup()`，实现只检查清单中的准确名称、必要时强制删除、再次确认不存在，并在持久记录成功清除后恢复。

隔离清单加载失败会持续失败关闭。因为容器执行前已经成功写入活动租约，后续原因更新失败也不会丢掉“可能存在容器”的持久标记。生产监控必须为这些情形设置高优先级告警；不要通过直接删数据库记录或手工“标记健康”跳过容器核对。

## 6. 配置文件

复制：

```text
config/suiteharness.example.yaml
config/suiteharness.secrets.example.yaml
```

形成公司实际的两份 YAML。加载方式：

```python
from suiteharness.config import SuiteHarnessConfigLoader

loaded = SuiteHarnessConfigLoader().load(
    "/etc/suiteharness/suiteharness.yaml",
    "/etc/suiteharness/suiteharness.secrets.yaml",
)
```

加载器只读取这两个显式路径，不做环境变量插值或命令替换。公开配置与密钥配置必须是
不同普通文件（硬链接到同一文件也拒绝），最大默认 1 MiB。`production` 还会拒绝常见
`replace-with-*`/`change-me` 占位符、全零镜像摘要、明显伪密钥及保留的 `example.*` 域名；
示例配置故意不能未经替换就上线。

关键设置：

- `deployment.mode` 只能为 `server`；
- `deployment.tenant_id` 固定当前客户公司；
- `server.host/port` 是公司 ASGI 监听地址；即使只启用飞书 Webhook 或只暴露健康路由也使用这一段；连接上限、backlog（监听队列）、启动和关闭超时也在 `server` 配置；
- `customer_bundle.products` 决定 A/AB/ABC；
- `customer360.mode` 默认 `isolated`；
- `workspace.root` 和 `storage.root` 使用独立绝对目录；
- `storage.session_limits` 限制会话数、转录事件、单事件正文、运行 claim 和检查点；转录/claim 达限时拒绝新工作，检查点则裁剪为最新的条数与字节窗口；
- `workspace.shared_enabled=false` 默认关闭共享工作区；启用时仍须用 `shared_access_by_product` 逐产品声明 `read_only` 或 `read_write`；
- `sandbox.network.default` 固定 `none`；
- `sandbox.network.allowed_profiles_by_product` 逐产品开放 Bash 出口；仅配置 Docker network 名称不会自动授权产品使用；
- `web_tools.search_providers_by_product` 和 `fetch_routes_by_product` 控制各产品可选择的搜索/抓取出口；未配置时只允许公司默认值，空列表表示连默认值也拒绝；
- `channels.web.allowed_origins` 只列公司 HTTPS 前端；`session_path` 默认 `/auth/session`，`session_lifetime_seconds` 只能在 30–900 秒之间且默认 300 秒；公司认证适配器总时限 `authentication_timeout_seconds` 默认 10 秒、最大 60 秒；空闲 WebSocket 的 `session_revalidation_interval_seconds` 默认 30 秒、最大 300 秒且不能超过票据寿命；WebSocket 入站帧在协议层和应用层固定为 1 MiB；WebSocket、会话交换、飞书 Webhook 和健康路径不能冲突；
- 飞书启用时必须填写公司应用 `app_id`，并固定 `default_access=read_only`、`interactive_approval=false`、`allow_delete=false`；公司目录认证时限 `authentication_timeout_seconds` 默认 10 秒、最大 60 秒；`event_processing_timeout_seconds` 是 claim 后分发、回复和完成标记的总上限（默认 600 秒），`event_processing_lease_seconds` 是阻止其他实例重复领取的 processing lease（默认 660 秒），配置必须满足 `lease > timeout`，否则加载失败；
- `channels.share_conversation_sessions=false` 默认按用户隔离持久会话；只有明确需要群组共享且完成成员授权时才开启；
- `channels.authorization_timeout_seconds` 同时约束产品 ACL 与共享 Web 会话成员 ACL，默认 10 秒、最大 60 秒；超时和适配器异常都拒绝；
- MCP 按产品配置；
- 每个 MCP 服务的 `max_message_bytes` 默认 4 MiB、范围 1 KiB–64 MiB，`max_stream_events` 默认 256、范围 1–10000；工具发现另有 `discovery_timeout_seconds`（默认 120 秒、上限 600 秒）、`max_list_pages`（默认 100、上限 1000）和 `max_list_items`（默认 10000、上限 100000）；按实际服务最小化，不要把上限当目标值，任一预算超限都会失败关闭；
- 插件只列显式路径和摘要，生产要求签名。

不要在 YAML 中存产品激活器 Python 路径后直接动态导入未验证代码；激活器由可信宿主从已验制品中选择。

## 7. 产品组合和工作区

### A

```yaml
customer_bundle:
  products:
    - product_id: product-a
      version: "==1.0.0"
      config: {}
  customer360:
    mode: isolated
    rules: []
```

### AB/ABC

增加产品选择，并分别配置：

- 产品描述和可信激活器；
- 模型路由；
- `channels.agent_ids`；
- Web/飞书会话路由；
- 工具授权模板；
- 产品工作区；
- 产品 MCP 服务；
- 独立画像/知识存储。

每个选中产品必须调用：

```python
foundation.prepare_product_workspace(product_id)
```

它会创建产品目录并注册 Web/internal/飞书不同的路径策略。

每个产品默认只读写自己的 product 根，shared 根默认不可见。共享工作区与画像的 `customer360.curated` 是两套独立开关；不要因为共享文件就放开画像，反之亦然。AB 只让 A 向共享区发布、B 只消费时可配置：

```yaml
workspace:
  root: /var/lib/suiteharness/workspaces
  shared_enabled: true
  shared_access_by_product:
    product-a: read_write
    product-b: read_only
```

未列出的 `product-c` 完全看不到 shared。飞书要写 shared，还必须同时满足该产品为 `read_write`、`channels.feishu.writable_roots` 精确白名单和飞书工具模板允许 `suiteharness.fs.write/edit`；任一项缺少都拒绝。

默认不要配置共享规则。只有审批过的字段才使用 `curated`，且每条规则准确写出生产方、消费方和字段。

## 8. 搜索和网页抓取的国内部署

### 8.1 搜索

默认示例使用百度千帆结构化 Web Search API，密钥放 `services` 密钥映射。另一选项是 Microsoft Foundry Bing，但需要部署项目注入 `FoundryGroundingClient`。

不要通过抓取百度/Bing 搜索结果 HTML 实现搜索；页面结构、反爬策略和结果完整性都不稳定。

### 8.2 抓取

可选：

- `direct`：公司服务器直接访问，仍做 DNS 固定和 SSRF 防护；
- `managed_proxy`：通过明确公司 HTTP CONNECT 代理；
- `browser_worker`：把需要 JavaScript 的页面交给独立浏览器工作进程。

境内访问不稳定时，优先部署公司管理的境内代理或浏览器工作器，并给它们同等的目标地址限制、审计、超时和结果大小限制。不能把代理当成绕过 SSRF 的后门。

## 9. 模型供应商

在 `models.profiles` 定义服务器模型配置，在 `models.routes` 定义主/后备路由，在 `product_routes` 选择 A/B/C 的路由。密钥只写在 secrets 文件对应引用中。

逐模型的能力差异写入 `models.profiles[].capabilities`。只有 profile 绑定一个精确模型并显式
设置 `allow_capability_overrides: true`，才能把厂商注册表默认关闭的能力提升为 `true`；这只是
管理员对已验证线协议的声明，不会让模型凭空获得能力。使用 DashScope 视觉模型做 JSON Schema
结构化输出时，还必须设置 `provider_options: {enable_thinking: false}`。OpenAI 兼容适配器只允许
透传这个布尔选项，拒绝任意请求体字段；遗漏时结构化请求在本地失败关闭。

部署前逐模型验证：

- 文本和中文编码；
- 工具调用及并行调用；
- 流式事件；
- 多模态；
- 超时、限流、重试；
- 模型名称和区域端点；
- 数据保留与跨境条款。

框架注册表中的 provider 能力不是厂商 SLA（服务等级承诺）。Ollama/vLLM 只允许连接公司管理的服务端点，不表示支持个人电脑部署。

## 10. 外部适配器清单

根据启用功能，部署项目要提供：

| 功能 | 需要的宿主适配器 |
| --- | --- |
| 企业 Web | `CompanyHttpAuthenticator` 验证已有公司 SSO/OIDC/JWT 登录态并返回内部 `AuthenticatedPrincipal`；共享会话再提供 `ConversationAuthorizer` |
| 产品访问控制 | 可选 `ProductAccessAuthorizer`；按主体、角色、渠道和路由后的产品逐消息查询公司 IAM/ACL。未提供时表示组合内所有已认证员工均可使用全部已购产品 |
| 飞书 | `CompanyIdentityDirectory`、出站发送器；Webhook 可选解密器或长连接官方 SDK |
| Foundry Bing | `FoundryGroundingClient` |
| 浏览器抓取 | `BrowserWorkerClient` |
| 插件生产签名 | `SignatureVerifier` 和可信密钥 |
| 隔离插件 | 对应 `PluginLauncher` |
| MCP stdio | `SandboxedStdioSessionFactory` |
| MCP HTTP | `McpEgressPolicy`、`McpHttpExchange`、必要时令牌提供器 |
| Bedrock/Vertex | 公司云凭据和相应可选依赖 |
| 产品 | 静态描述、可信激活器、生产画像/知识 provider |

缺少已配置功能所需适配器时，服务器扩展/渠道组合器会拒绝启动。

## 11. 正确的服务器装配顺序

推荐用 `CompanyServerBootstrap`（公司服务器启动组合器），不要在部署项目中复制低层装配顺序。最小组合模块如下；示例中的变量都是部署方从经过审核的公司代码中构造，不是让用户或 YAML 指定的导入路径：

```python
from suiteharness.server import (
    CompanyServerBootstrap,
    create_company_asgi_app,
)

bootstrap = CompanyServerBootstrap.from_files(
    "/etc/suiteharness/suiteharness.yaml",
    "/etc/suiteharness/suiteharness.secrets.yaml",
    product_catalog=verified_product_catalog,
    product_activators=verified_product_activators,
    grant_templates=reviewed_grant_templates,
    web_authenticator=company_http_authenticator,
    web_conversation_authorizer=company_conversation_authorizer,
    product_access_authorizer=company_product_acl,
    feishu_adapters=feishu_host_adapters,
    plugin_adapters=plugin_host_adapters,
    mcp_adapters=mcp_host_adapters,
    foundry_clients=foundry_clients,
    browser_workers=browser_workers,
)

# 导出给公司 ASGI 平台；也可在单进程受控环境中调用 application.run()。
application = create_company_asgi_app(bootstrap)
```

只传启用功能所需的可选适配器：Web 关闭时不能传 `web_authenticator`；飞书关闭时通常不传 `feishu_adapters`；未配置插件/MCP/Foundry/浏览器工作器时相应项可省略。Web 开启则必须传 `CompanyHttpAuthenticator`；共享 Web 会话还必须传可信 `ConversationAuthorizer`。若不同部门只能使用部分产品，Web 和飞书共用的 `ProductAccessAuthorizer` 必须连接公司 IAM/ACL；它在产品路由之后、创建作用域之前逐消息执行，拒绝、异常、超时或非严格 `True` 都失败关闭。两个 ACL 适配器共用 `channels.authorization_timeout_seconds`。省略产品 ACL 等价于“全部已认证公司用户可访问组合内全部产品”，部署评审必须明确接受。飞书必须使用 `FeishuHostAdapters(directory, outbound_sink, webhook_decryptor=...)` 或 `FeishuHostAdapters(..., long_connection_sdk=...)`，按配置传输二选一。

Foundry 搜索配置只声明固定的 `provider_id`；项目端点、Bing 项目连接和 Microsoft Entra（微软
云身份）认证由可信宿主在预绑定 `FoundryGroundingClient` 时提供，不写入 SuiteHarness 的通用
service secrets，也不能用任意非空 token 冒充认证成功。宿主应使用公司托管身份与 RBAC（基于
角色的访问控制）或等价服务器凭据，并确保客户端绑定到预期项目和连接。框架启动时会拒绝缺失
或未实现 `grounded_search` 的客户端，运行时仍按 `provider_id` 执行产品工具授权和出口隔离。
目前宿主负责创建该客户端；未来需要按配置懒创建时，应另行实现显式 factory（工厂）接口。

`build()` 内部顺序固定为：解析客户组合 → 创建唯一 Web 审批回路 → `FoundationRuntime.create()` 并探测底座 → 准备产品工作区 → 激活产品 → 启动插件/MCP → 按已发现 MCP 清单组合渠道 → 启动可选飞书长连接 → 聚合就绪。任何阶段失败都反向回滚并保持不接流量；标准路径还把 `foundation.runtime_database` 复用给渠道去重。只有需要实现自定义公司宿主时才直接使用 `FoundationRuntime`、`CompanyChannelRuntime` 等低层接口。

框架不会自动发现 `verified_product_catalog`，也不会替公司生成 `verified_product_activators` 或 `reviewed_grant_templates`。这三项决定 ABC 中实际加载的代码和工具权限，必须来自可信制品及管理员评审。

## 12. 工具授权模板

`CompanyChannelRuntime.create()` 要求模板集合**准确覆盖**每一个启用渠道 × 选中产品。例如 Web + 飞书的 ABC 必须有六个模板。

安全基线示例：

```python
READ_TOOLS = (
    "suiteharness.fs.list",
    "suiteharness.fs.read",
    "suiteharness.fs.glob",
    "suiteharness.fs.grep",
    "suiteharness.web.search",
    "suiteharness.web.fetch",
)

templates = (
    ToolAccessTemplate(
        channel_id="web",
        product_id="product-a",
        read_aliases=READ_TOOLS,
        write_aliases=("suiteharness.fs.write", "suiteharness.fs.edit"),
        destructive_aliases=("suiteharness.shell.bash",),
    ),
    ToolAccessTemplate(
        channel_id="feishu",
        product_id="product-a",
        read_aliases=READ_TOOLS,
        write_aliases=(),              # 默认全局只读
        destructive_aliases=(),
    ),
)
```

解释：

- Web 写和 Bash 仍会触发逐调用交互审批；
- 飞书若要开放写入，只能把 `suiteharness.fs.write/edit` 放进 `write_aliases`，同时配置至少一个 `writable_root`；
- 飞书永远不能有 `destructive_aliases`；
- 工具效果变化或身份过期会失败关闭。

MCP 权限不混入 `ToolAccessTemplate`。唯一真相是 YAML 中按渠道、产品、服务和远端原始工具名声明的白名单，默认空即全部拒绝：

```yaml
mcp:
  # 假设 product-a 已在 servers_by_product 中配置 docs 服务。
  tool_security_overrides:
    product-a:
      docs:
        search_docs:
          effect: read
          rationale: "只执行公司文档检索，不修改远端状态"
  channel_access:
    web:
      product-a:
        allow_tools:
          docs: [search_docs, export_report]
    feishu:
      product-a:
        allow_tools:
          docs: [search_docs]
```

- `allow_servers: [docs]` 表示允许 `docs` 当前发现的全部工具，适合经过整体审查且工具集合受控的服务；
- `allow_tools.docs: [search_docs]` 只允许该服务的远端原始工具名，推荐用于最小授权；
- 同一服务不能同时出现在 `allow_servers` 和 `allow_tools`；
- 精确工具名在启动发现清单中不存在会拒绝启动；每次授权租约还会复核准确身份和能力，防止热变化扩大权限；
- 默认 MCP 工具被视为写 + 外部访问。Web 可显式选择，但每次写/破坏性调用仍审批；
- 飞书选中的 MCP 工具必须经带理由的 `tool_security_overrides` 最终注册为只读，否则拒绝授权。

## 13. WebSocket 和企业 SSO

内置公司 ASGI 应用提供 `session_path`（默认 `POST /auth/session`），把浏览器已有的公司登录态换成 SuiteHarness 短时 WebSocket 票据。它不是登录页，也不接收账号密码正文。部署方最小认证适配器可以是：

```python
from suiteharness.channels import AuthenticatedPrincipal
from suiteharness.server import (
    CompanyAuthenticationUnavailable,
    CompanyHttpAuthenticationRequest,
)


class CompanySsoAdapter:
    async def authenticate(
        self,
        request: CompanyHttpAuthenticationRequest,
    ) -> AuthenticatedPrincipal | None:
        try:
            account = await company_iam.verify_existing_session(
                authorization=request.header("authorization"),
                cookie=request.header("cookie"),
                csrf_token=request.header("x-csrf-token"),
                origin=request.origin,
            )
        except CompanyIamTemporaryError as exc:
            raise CompanyAuthenticationUnavailable from exc

        if account is None or account.disabled:
            return None
        return AuthenticatedPrincipal(
            tenant_id="company-a",  # 必须等于 deployment.tenant_id
            principal_id=account.stable_subject,
            roles=frozenset(account.suiteharness_roles),
        )
```

`company_iam` 和 `CompanyIamTemporaryError` 是示例占位符；真实实现必须校验公司 SSO/OIDC/JWT、Cookie 场景的 CSRF、账号禁用、角色和固定 tenant，不能相信浏览器自报字段。正确流程：

1. 用户完成企业 SSO/OIDC/JWT；
2. 浏览器从精确允许的 HTTPS Origin，以已有 Cookie 或 Authorization（认证）头调用 `POST /auth/session`，请求正文必须为空；
3. 内置路由把受边界限制的请求元数据交给认证适配器，并核对返回的 tenant；
4. 成功响应为 `{access_token, token_type: "Bearer", expires_in}`，禁止缓存，同时设置限定 `/ws` 路径的 HttpOnly/SameSite=Strict/Secure 短票 Cookie；票据寿命默认 300 秒、只能配置 30–900 秒；
5. 浏览器自动携带 Cookie 连接 `/ws` 并使用 `suiteharness.v1` 子协议；不能设置 WebSocket `Authorization` 头的限制不会迫使前端把票据写入 URL 或 localStorage。非浏览器客户端仍可使用响应中的 Bearer；
6. 框架在每个非断开客户端帧前重验原票据；票据过期时关闭连接、取消连接内在途运行，并要求重新经过公司认证流程。

`OPTIONS` 预检只接受目标方法 `POST` 并返回精确 Origin。路由拒绝正文、重复安全头、超量/非法头和不允许的 Origin；认证不可用返回 503，错误凭据或异租户主体返回 401。不能让浏览器自己提交 `tenant_id`、`principal_id` 或角色后直接签票。

`web/default.session_signing_key` 至少 32 个字符，生产必须使用密码学随机值并与公开配置分离。当前 HMAC（基于哈希的消息认证码）短票没有 key-id（密钥编号）多密钥轮换、内置吊销存储、刷新或重放存储；认证/动态复核使用 10 秒默认、60 秒硬上限的调用超时，但框架没有内置熔断或分布式限流。部署可向 `CompanyServerBootstrap(web_session_revalidator=...)` 注入实现了 `WebSocketSessionRevalidator` 的公司适配器：它根据握手凭据摘要和初始主体查询退出/吊销状态、账号启用状态及当前角色，返回最新 `AuthenticatedPrincipal`；返回空值、tenant/主体/角色改变、超时或异常都会关闭连接。适配器不得记录或持久化原始 Cookie/Bearer。生产还要完成短 TTL、密钥轮换、CSRF、速率限制、登录审计和异常保护。

### 13.1 可选共享会话

默认 `share_conversation_sessions=false`：即使用户位于同一 Web conversation 或飞书 chat，持久 `SessionIdentity` 仍由各自 `principal_id` 隔离。

显式改为 `true` 后，服务器生成绑定“公司 + 渠道 + conversation + 产品”的 synthetic session owner（合成会话所有者），授权成员会真正看到共同多轮历史。必须接受以下要求：

- Web conversation 标识可由客户端指定，所以必须注入可信 `ConversationAuthorizer`（会话成员授权器）；
- authorizer 每次消息都依据公司 ACL（访问控制列表）核对“主体 + conversation + 产品”，非严格 `True`、失败、不可用或超过配置超时时拒绝；
- 飞书 chat 标识来自已验签事件，用户仍必须经过企业目录认证；
- 工具 grant、审批和审计的发起人仍是当前真实主体，不是合成所有者；
- 成员加入、退出、离职、历史可见期和数据导出由公司 IAM/会话系统治理。

服务器多轮上下文只从持久转录重建成功配对的 user/assistant 消息；metadata、附件内容、失败/取消/未配对轮次不会进入模型历史。默认最多扫描 256 个事件、选择 32 条消息和 128 KiB，超限从较旧轮次截断。共享会话在送入模型的提示词窗口中只使用 `participant-1` 等临时别名，不发送其他成员的真实 `principal_id`；客户端也不能上传一段自报历史替换它。

`CompanyServerApplication` 在 lifespan 启动完成后才动态加入 WebSocket 和飞书 Webhook 路由并接收流量，退出时移除路由并逆序清理。需要自定义公司服务器时，才直接使用低层 `channels.starlette_routes()` 组合自己的 ASGI 应用。

## 14. 飞书装配

```python
feishu_host_adapters = FeishuHostAdapters(
    directory=company_feishu_directory,
    outbound_sink=company_feishu_outbound_sink,
    bot_open_id=configured_bot_open_id,
    # Webhook 加密时提供：
    webhook_decryptor=company_webhook_decryptor,
    # 或长连接时提供官方 SDK 适配器，两者按 transport 二选一：
    long_connection_sdk=company_long_connection_sdk,
)
```

- `directory` 把飞书用户标识映射为公司内部主体及角色；
- `outbound_sink` 一般包装 `FeishuMessageClient`；
- Webhook 模式要求签名/verification token，并在启用加密时提供解密器；
- 长连接模式必须提供官方 SDK 适配器；
- 渠道组合器使用传入的 `foundation.runtime_database` 创建持久飞书事件去重器；该连接仍由 Foundation 拥有和关闭；
- `CompanyServerBootstrap` 从 `channels.feishu` 把 `event_processing_timeout_seconds` 同时接到事件处理器、把更长的 `event_processing_lease_seconds` 接到 SQLite 去重器。处理超时会取消当前分发并释放该代 claim，让飞书按供应商重试策略重新投递；租约覆盖整个允许处理窗口，因此首个处理仍运行时其他进程不能领取同一事件。产品若直接组合低层 `FeishuEventProcessor`，也必须提供公开 `processing_lease_seconds` 且严格大于处理上限的去重器；
- 官方 SDK 适配器自身的回调等待上限不得小于 `event_processing_timeout_seconds`，应从同一产品/部署配置取值，不能另写一个更短的硬编码值。
- 开启共享会话时，同一已验签 `chat_id + product_id` 的成员使用共同持久历史；这不放宽飞书工具策略。

标准 `CompanyServerBootstrap` 会在受监管后台任务中启动长连接，并在任务立即失败时让启动失败；Webhook 路由则在 ASGI lifespan 成功后动态加入。直接使用低层 `CompanyChannelRuntime` 的自定义宿主才需要自行调用 `run_feishu_long_connection()` 或组合 `starlette_routes()`。无论哪种传输，飞书权限规则完全相同。

## 15. ASGI、反向代理和就绪

`create_company_asgi_app(bootstrap)` 返回可调用的 `CompanyServerApplication`：

- `/health/live` 始终返回 200，只表明 ASGI 事件循环可响应；
- `/health/ready` 在 runtime 尚未建立、正在关闭或任一检查失败时返回 503；正常时检查 Foundation 启动状态、沙箱、运行数据库、会话、审计、插件、MCP、渠道、产品 activation 和可选飞书长连接，并只公开每项 `ok/failed`；
- 健康路由应只暴露给编排器或私网监控；在反向代理/服务网格为 `/health/live` 和 `/health/ready` 设置独立速率与并发限制，不要把匿名高频探测直接暴露到公网；
- Web 开启时注册配置的会话交换路由；完成 lifespan 装配并通过就绪检查后才加入 WebSocket/飞书 Webhook 路由；
- 启动或关闭受 `server.startup_timeout_seconds` / `shutdown_timeout_seconds` 限制，失败会反向清理。

便捷单进程运行：

```python
application.run()          # 阻塞运行 Uvicorn
# 或在已有事件循环中：
await application.serve()
```

两者使用顶层 `server.host/port/max_concurrent_connections/backlog`，强制 Uvicorn lifespan，并设置 `proxy_headers=False`。生产更推荐在公司的 ASGI 平台托管同一个可调用 `application`，由平台管理工作进程、TLS 和完整停机参数。仓库没有预制全局 `app` 或命令行脚本，是因为可信产品和公司适配器不能安全自动推断；这不等于框架缺少 ASGI 应用工厂。

内置 readiness（就绪检查）不主动调用模型厂商、搜索/抓取出口、公司 SSO 或飞书出站 API；插件也没有统一自定义深度健康探针。Docker daemon 探测在每个进程内合并并发请求，并短暂缓存结果（健康 3 秒、失败 0.25 秒），但隔离区状态始终优先、逐次检查。该机制只减少子进程探测放大，不能替代外围访问控制与限流。飞书长连接只检查后台任务是否仍运行，意外退出会降为未就绪并记录严重日志，但不会自动重启。多 worker 会各自启动一条长连接，部署层必须采用单消费者或外部协调。

反向代理必须：

- 终止或透传公司 TLS；
- 保留 WebSocket upgrade；
- 限制请求体和连接时长；
- 不改写为未在 allowlist 中的 Origin；
- 正确获得客户端 IP，但不盲信公网传入的转发头；
- 对健康检查、登录、票据签发和 Webhook 分别设置独立速率与并发限制。

指标、管理接口、WAF（Web 应用防火墙）和额外健康探针由部署项目增加，并与业务入口分离认证。

## 16. 持久化和高可用

当前自动接线：

- SQLite 会话；
- SQLite 审计；
- Foundation 拥有的 SQLite 运行数据库；
- 运行数据库中的通用产品状态、MCP HTTP 恢复游标和 Docker 清理隔离清单；
- 复用同一运行数据库的飞书渠道事件去重（飞书开启且按标准装配时）。

会话容量由公开配置的 `storage.session_limits` 统一传入内存与 SQLite 实现。转录事件和运行 claim 保留消息幂等/副作用防重证据，达到单会话或总量上限时会失败关闭，不能把调大上限当作数据保留策略；应先告警，再由运维执行归档、迁移或容量扩展。检查点是可替换快照，会在同一事务内按 `max_checkpoints_per_session` 与 `max_checkpoint_bytes_per_session` 删除最旧项，同时单个正文仍受独立字节上限约束。

每条渠道消息还会原子 claim 一个稳定 `run_id`。不同服务器进程不能同时获得同一执行权；租约在终态落盘前过期时，该运行永久进入“不确定、需人工核对”路径，不会把所有权交给重投消息后再次执行工具。这是安全的 at-most-once（至多一次）取舍，不承诺每个崩溃请求都自动得到最终答案。

不要省略装配示例中的 `runtime_database=foundation.runtime_database`。省略时渠道组合器会为飞书自行打开配置路径，虽然仍可工作，却形成第二个连接和另一套关闭所有权，不是推荐的统一生命周期。

MCP Streamable HTTP 只有在对应服务器配置 `resume_sessions=true` 时使用恢复存储。键绑定公司、产品、服务器和配置端点的 SHA-256 摘要，值只含 `session_id/last_event_id`；令牌、认证头和端点 URL 不入库，出口策略也不得把 URL 改写到其他端点。每次请求/通知后都用 CAS 更新，持久化失败会禁用连接。开启恢复时停机保留远端会话，未开启时正常关闭会发送 `DELETE`。运维必须测试远端会话过期、端点变更、数据库冲突/不可用及重新初始化策略。

画像、Customer360 和词法知识内置版本是进程内实现；生产产品必须提供持久 provider。

单服务器可把 SQLite 放在本机持久盘并配置备份。不要把 SQLite 文件放到语义不兼容的共享网络文件系统。多实例需要替换：

- 能力授权机构；
- 审批存储及 Web 审批协调；
- 工具/贡献注册协调；
- 会话/审计/产品状态/MCP 恢复/沙箱隔离/渠道去重数据库；
- 激活和任务租约；
- 分布式速率限制与幂等去重。

仅把两个进程指向同一个 SQLite 文件不构成完整高可用方案。

## 17. 网络出口

默认所有 Docker 工具断网。需要外网时：

1. 运维预建命名 Docker network；
2. 配置 `sandbox.network.egress_profiles` 名称映射；
3. 工具请求只能选允许 profile；
4. 出口节点执行域名/IP/端口策略、DNS 控制、TLS 和日志；
5. 凭据由服务器注入窄服务，不放用户命令或工作区。

模型、搜索、抓取、MCP 和 Bash 是四种不同出口，应分别控制。允许模型 API 不意味着允许 Bash 任意联网。

配置还要做第二层产品授权：

```yaml
sandbox:
  network:
    default: none
    egress_profiles:
      china-web: suiteharness-egress-cn
    allowed_profiles_by_product:
      product-a: [china-web]

web_tools:
  search_providers_by_product:
    product-a: [baidu-qianfan]
    product-b: []             # B 连公司默认搜索也不能用
  fetch_routes_by_product:
    product-a: [direct]
```

Bash 未列出的产品只能断网；Web 搜索/抓取未列出的产品只能选公司默认 provider/route，选择非默认值必须列入该产品。显式空列表用于完全拒绝该类 Web 出口。配置加载器会拒绝未知产品和未定义出口名称。

## 18. 启动失败与停机

ASGI lifespan 会在开始接收渠道流量前完成配置、Foundation、产品、插件、MCP、渠道和就绪检查。任一步失败都回滚并保持未就绪；不要绕过 `CompanyServerBootstrap.build()` 先手工开放 WebSocket/Webhook 路由。

内置停机顺序：

1. 从负载均衡摘除并拒绝新连接；
2. `CompanyServerApplication` 把 accepting（接收流量）切为 false；
3. 关闭 `CompanyChannelRuntime` 和 Web 审批，再取消飞书长连接任务；
4. 关闭 Foundation：先扩展和内核、排空运行，再关闭工具、会话、审计、模型传输和所拥有的运行数据库；
5. 无论关闭是否报错，动态渠道路由都会移除，错误继续上报；
6. 等待日志/指标刷新；
7. 超时后由外部 supervisor（进程监管器）处理不合作进程。

使用应用 lifespan 时不要再重复关闭 channels、产品 activation 或 Foundation；保持一个清晰所有者。直接使用低层对象的自定义宿主才需要自行复制相同的反向顺序。

## 19. 监控与审计

至少采集：

- 渠道认证失败、路由失败和重复事件；
- 会话 CAS 冲突及不确定运行；
- 每产品模型时延、错误和重试；
- 工具允许/拒绝/审批/超时及参数脱敏摘要；
- Docker 创建、超时清理、持久隔离状态、管理员核对结果和出口 profile；
- MCP 连接、能力协商、重连、任务、恢复游标修订和持久化错误；
- 插件摘要、签名、版本、启动/替换/回滚；
- SQLite 容量、锁等待、备份和恢复；
- WebSocket 连接、帧拒绝和审批超时。

审计读取接口要与业务入口分离。原始用户内容、密钥和审批令牌不得直接进入普通指标标签。

## 20. 上线前验收

- [ ] 客户公司独立 tenant、服务、密钥、状态和工作区；
- [ ] 依赖已从受控源安装、锁定、扫描并生成 SBOM；
- [ ] 生产采用 Linux + Docker，并确认启动日志/就绪门未报告安全工作区后端缺失；
- [ ] 沙箱基础镜像和最终镜像都固定真实摘要；
- [ ] 沙箱用户固定 `65532:65532`；
- [ ] 仅准确工作区通过属主/ACL 可写，未使用 `chmod 777`；
- [ ] shared 默认关闭；若启用，逐产品只读/读写映射及飞书额外白名单已评审；
- [ ] Docker socket 未挂入容器，已评估 rootless/隔离节点；
- [ ] Docker 隔离清单持久可用，清理失败告警和准确名称核对流程已演练；
- [ ] Bash、搜索和抓取的可选出口按产品逐项审查，空列表/默认值语义已测试；
- [ ] 公司 SSO、票据、Origin、撤销和密钥轮换完成；
- [ ] 若开启共享会话，Web 已注入逐消息成员 authorizer，飞书共享范围和成员治理已评审；
- [ ] 飞书目录、签名、解密/SDK、去重和出站发送完成；
- [ ] 每个启用渠道 × 产品都有且只有一个工具模板；
- [ ] 飞书默认只读；写只限 `fs.write/edit` 白名单；删除永禁；
- [ ] Web 写入、Bash和其他破坏性动作走一次性审批；
- [ ] 每产品画像/知识/工作流/提示词/反思/MCP 均验证隔离；
- [ ] `customer360` 默认为 isolated；curated 规则有治理依据；
- [ ] 搜索使用百度/Foundry 结构化接口；
- [ ] 抓取/代理/浏览器工作器通过 SSRF 和出口测试；
- [ ] 模型厂商逐模型做契约与数据合规测试；
- [ ] 插件摘要、签名、权限和隔离启动器完成；
- [ ] MCP stdio/HTTP 传输、令牌、出口、消息/流事件边界和降权规则完成；逐渠道产品白名单已验证；恢复模式已验证端点绑定、游标存储失败及远端过期；
- [ ] 会话、审计、画像、知识完成备份恢复与保留策略；
- [ ] 失败启动、消息重投、进程崩溃和优雅停机完成演练；
- [ ] 已接受 [安全模型](security-model.md) 中全部当前边界。
