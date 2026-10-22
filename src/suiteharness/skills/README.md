# 通用 Skill Provider

本目录只提供产品无关的技能读取机制。业务说明与 Skill 文件保留在产品仓库。
运行中的 prepared/active 证据由 `runtime.py` 管理；任何 provider 读取结果本身都不是操作授权。

## 本地包

```python
provider = FilesystemSkillProvider(
    product_skill_directory,
    namespace=product_id,
    access_policy=optional_employee_policy,
)
```

只扫描显式登记根目录中的 `SKILL.md`。必填元数据为 `name`、`description`、
`version`；可选为 `display_name`、`includes`、`user-invocable`、
`disable-model-invocation`。未知、重复字段、YAML 别名、循环依赖和不完整资源失败，
不静默忽略。`includes` 是同包名称，不是文件路径。

元数据中的 invocation 限制只控制入口：不可由模型单独选择的公共规约仍可由子 Skill
引用，但每一个依赖仍单独经过 `access_policy`。回复资源为同目录的 `REPLY.md`，
只能通过 `source="workflow"` 读取。

`static_manifest()` 和同步 `compile_resource()` 只用于可信主机初始化、校验和旧产品
同步接口的迁移；它们不伪造员工身份、不创建运行期加载凭证，不能开放成任意文件读取工具。
本地包启动时冻结元数据和正文，读取时核对原始文件摘要；修改后须重新加载产品。

自定义 `SkillProvider` 若供工具声明 `required_skills`，必须覆写
`static_manifest()` 提供可信、同步、无网络的注册清单；否则产品激活会因引用未登记
的技能而失败。默认空清单只适用于不声明技能前置条件的产品，不代表跳过校验。
清单不是员工可见目录，更不授予读取或业务执行权限。

## MCP 远程包

这不是所谓的 MCP 原生 Skill 标准。当前适配器通过标准 `tools/call` 使用明确的应用契约。
连接、TLS/网络限制、认证和服务生命周期复用部署配置中的 `McpClient`，模型不能传入 URL、
认证字段或远端工具名。

```python
provider = McpSkillProvider(
    configured_client,
    namespace=product_id,
    package_id="approved-package",
    manifest=reviewed_descriptors,
    resource_hashes=reviewed_resource_hashes,
    list_tool="skills.list",
    read_tool="skills.read",
    access_policy=optional_employee_policy,
    timeout_seconds=10,
)
```

### 通过现有连接管理器接入（推荐）

宿主一般持有 `McpClientManager` 而不是内部连接对象。可直接使用公开工厂：

```python
from suiteharness.skills import SKILL_PROVIDER, McpSkillProvider

provider = McpSkillProvider.from_manager(
    existing_mcp_manager,
    tenant_id=tenant_id,
    product_id=product_id,
    server_id="approved-skill-service",
    package_id="approved-package",
    manifest=reviewed_descriptors,
    resource_hashes=reviewed_resource_hashes,
    access_policy=optional_employee_policy,
)
product_bindings[SKILL_PROVIDER] = provider
```

这个方式使用 `BoundMcpSkillClient` 固定部署选定的 tenant/product/server，调用真正的：

```python
await manager.call_tool(real_request_scope, fixed_server_id, fixed_tool_name, arguments)
```

保留管理器原有的连接归属查找、连接状态、认证、传输与出站网络检查；不访问
`manager._clients`、不获取内部 client、不伪造员工 Scope、不自动创建或重连服务。
部署宿主应按现有 MCP 生命周期先配置并连接该产品的服务；连接不存在或断开时明确失败，
关闭连接仍由原管理器负责，Skill provider 不接管它。

也可由产品激活代码显式调用严格配置工厂：

```python
from suiteharness.skills import SKILL_PROVIDER, mcp_skill_provider_from_config

# reviewed_skill_config 是本产品审核后的配置映射，不是模型工具参数。
product_bindings[SKILL_PROVIDER] = mcp_skill_provider_from_config(
    existing_mcp_manager,
    tenant_id=tenant_id,
    product_id=product_id,
    config=reviewed_skill_config,
    access_policy=optional_employee_policy,
)
```

配置映射示意如下（省略实际清单值，摘要占位符不可直接使用）：

```yaml
server_id: approved-skill-service
package_id: approved-package
list_tool: skills.list
read_tool: skills.read
timeout_seconds: 10
manifest: []        # 必须填写下文格式的完整、已审核 descriptor 清单
resource_hashes: {} # 必须逐项填写下文格式的真实资源摘要
```

它只接受这七个字段，必填为 `server_id`、`package_id`、`manifest`、`resource_hashes`。
URL、密码、令牌、`credential_ref`、员工 ID 与 roles 均不允许放在这个映射中。
端点和认证仍从现有 MCP 部署配置与凭据管理取得。这个工厂不是新增全局 YAML 字段，
产品/宿主需明确调用并绑定；不能将这段内容任意粘到严格的顶层配置模型中就期待自动启用。

专用 Skill 服务只通过 provider 的受控读取入口暴露，不应再让通用 MCP 工具桥接把
`skills.list/read` 作为另一套普通模型工具重复发布。使用远端源需要显式更改产品绑定，
不会因远端异常自动切到本地或别的服务。当前产品 A 保持本地文件源。

`manifest`、`resource_hashes` 是管理员审核的部署锁定清单，不从模型或一次未经审批的远端
发现直接批准。哈希提供完整性校验，不等于签名，也不代表该资源有业务权限。

`resource_hashes` 形式：

```json
{
  "product.task": {
    "instructions": "完整合成正文的64位小写SHA256",
    "reply": "内部回执正文的64位小写SHA256"
  }
}
```

每个登记 Skill 必须有 `instructions` 锁，只有确实提供回执时才配置 `reply`。
`instructions` 是按依赖拓扑后序、去重后用两个换行连接的最终正文。
可从已审核的本地包 `static_manifest()` 与 `compile_resource()` 输出制作锁定清单。
远端版本更新需显式更新登记和锁定清单，没有自动批准、过期缓存回退或其他来源切换。

### skills.list 契约

工具参数只包含部署决定的 `package_id`：

```json
{"package_id":"approved-package"}
```

返回 `structuredContent` 或单一 JSON text block：

```json
{
  "package_id":"approved-package",
  "skills":[{
    "skill_id":"product.task",
    "name":"task",
    "display_name":"任务",
    "description":"何时使用该技能",
    "version":"1.0.0",
    "source_sha256":"原始SKILL.md字节的64位小写SHA256",
    "model_invocable":true,
    "user_invocable":true,
    "includes":[]
  }]
}
```

示例中的摘要为说明占位符，真实返回必须为有效十六进制 SHA-256。
所有 descriptor 字段都必须出现，`display_name` 可为 null。数组只能是静态清单的子集，
不得新增对象、改描述、改版本或改摘要。公开目录还会与本地员工策略和 invocation 条件取交集。

### skills.read 契约

```json
{
  "package_id":"approved-package",
  "skill_id":"product.task",
  "resource_id":"instructions",
  "version":"1.0.0",
  "source_sha256":"已登记的原始摘要"
}
```

返回：

```json
{
  "package_id":"approved-package",
  "descriptor":{"与静态清单一致的完整descriptor":"见上文"},
  "resource_id":"instructions",
  "instructions":"完整公共规约与任务说明",
  "instructions_sha256":"完整合成正文的实际SHA256",
  "dependencies":["product.common"]
}
```

`dependencies` 必须与已登记依赖展开的完整后序去重列表一致，不含目标自身；内部 reply 为
空列表且只含 reply 自身正文。正文、版本、原始摘要和资源锁任一不匹配都停止依赖动作。

### 身份与失败边界

真实 `RequestScope` 传入既有 MCP 客户端，校验连接的 tenant/product 归属；这不等于自动
在远端建立了员工 OAuth 身份。远端默认使用已配置服务认证，本地员工可见性仍由
`access_policy` 强制执行。需要远端人员级权限时，应另行配置可信委托身份协议，不能相信
模型传入的姓名或 roles。本实现不自动外传个人画像，也不把身份放入模型工具参数。

超时、不可用、远端错误、Schema 错误与版本变更均返回结构化 `SkillError`；不隐式重试，
不回退旧成功内容，不把异常中的认证信息输出给模型。provider 借用客户端，不关闭共享
MCP 连接。测试使用假客户端，没有调用真实远端或发送业务消息。
