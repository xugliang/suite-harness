# 产品开发指南

本指南说明如何把一个业务能力开发为可加入 A、AB、ABC 客户组合的 SuiteHarness 产品。示例统一使用 `product-a`、`product-b`，不包含任何具体行业业务。

Product（产品）是独立的配置与运行边界；Customer Bundle（客户产品组合）是某家公司选择的一组产品；Runtime Bundle（产品内部运行能力包）是单个产品内部的依赖单元。三者不能混用。

## 1. 产品应包含什么

推荐一个产品源码目录只包含产品自身内容：

```text
products/product_a/
  descriptor.py       静态产品描述，不建立网络连接
  config.py           严格产品配置模型
  activator.py        可信准备与安装代码
  workflow.py         Agent 工作流
  prompts.py          提示词策略
  reflection.py       可选反思策略
  memory.py           画像/知识生产适配器
  tools.py            可选产品工具
  bundles.py          可选产品内部能力包
  tests/
```

产品不应复制 Harness 的认证、审批、审计、Bash、文件工具、Web 搜索/抓取、Docker 沙箱、模型厂商适配器或渠道服务器。这些能力由公司部署底座统一提供。

产品也不能假设公司 shared 工作区存在。每个产品默认只获得自己的 product 工作区；共享目录由部署配置默认关闭，即使启用也要逐产品赋予 `read_only`（只读）或 `read_write`（读写）。需要交换结构化用户事实时应走下文的 Customer360 策展流程，不要把共享文件夹当成绕过画像边界的数据库。

## 2. 第一步：定义严格配置和 ProductDescriptor

`ProductDescriptor`（产品描述）是可在不执行产品激活代码时读取的静态元数据。

```python
from pydantic import BaseModel, ConfigDict, Field

from suiteharness.runtime import ProductDescriptor


class ProductAConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    system_prompt: str = Field(min_length=1, max_length=20_000)
    knowledge_top_k: int = Field(default=5, ge=1, le=20)


PRODUCT_A = ProductDescriptor(
    product_id="product-a",
    version="1.0.0",
    harness_api=">=0.1,<0.2",
    capabilities=frozenset(
        {
            "product-a.catalog.read",
            "memory.private.read",
            "memory.private.write",
        }
    ),
    config_model=ProductAConfig,
)
```

规则：

- `product_id` 使用稳定、小写、可审计的标识，不用客户名称；
- 版本必须是规范 PEP 440（Python 版本规范）版本；
- `harness_api` 明确兼容范围；
- Pydantic（Python 数据校验库）配置必须 `extra="forbid"`，未知字段直接失败；
- `capabilities` 是产品可能需要的能力上限，不是运行授权；
- 产品激活器由公司部署源码显式传给 `CompanyServerBootstrap`；框架不会扫描 pip Entry Point 或按字符串自动导入产品代码。

例如 AB 同时选择 A、B 时，两者各自解析自己的配置模型，A 的字段不会被 B 接受或读取。

## 3. 第二步：准备产品能力

`ProductActivator`（产品激活器）是可信宿主选中的代码。它的 `prepare()` 返回 `PreparedProduct`（已准备产品），但准备阶段不能发布路由、工具、任务等外部可见副作用。

下面示例把工作流作为构造参数注入，使部署方可以为不同客户选择实现：

```python
from collections.abc import Callable
from typing import cast

from suiteharness.agents import DefaultPromptStrategy, NoOpReflectionStrategy
from suiteharness.execution import (
    PROMPT_STRATEGY,
    REFLECTION_STRATEGY,
    WORKFLOW,
    Workflow,
)
from suiteharness.memory import InMemoryProfileProvider, PROFILE_PROVIDER
from suiteharness.runtime import PreparedProduct, ResolvedCustomerProduct

from .config import ProductAConfig
from .descriptor import PRODUCT_A


class ProductAActivator:
    descriptor = PRODUCT_A

    def __init__(
        self,
        *,
        workflow_factory: Callable[[ProductAConfig], Workflow],
    ) -> None:
        self._workflow_factory = workflow_factory

    async def prepare(
        self,
        product: ResolvedCustomerProduct,
    ) -> PreparedProduct:
        config = cast(ProductAConfig, product.config.model_copy(deep=True))

        # 仅为示例。生产必须替换为按 tenant_id + product_id 分区的持久实现。
        profile = InMemoryProfileProvider()
        workflow = self._workflow_factory(config)

        return PreparedProduct(
            descriptor=self.descriptor,
            config=config,
            bindings={
                PROFILE_PROVIDER: profile,
                WORKFLOW: workflow,
                PROMPT_STRATEGY: DefaultPromptStrategy(
                    system_prompt=config.system_prompt,
                ),
                REFLECTION_STRATEGY: NoOpReflectionStrategy(),
            },
        )
```

`FoundationRuntime` 已在 Root 层绑定产品路由的默认 ReAct（Reason + Act，推理并行动）、默认提示词和无操作反思。产品若接受这些默认值，可以只绑定画像；产品若要改变行为，应在 Product 层显式覆盖相应策略。直接使用裸 `HarnessKernel` 的宿主则必须自行提供工作流绑定。

每个选中产品都必须提供一个独立存活的 `PROFILE_PROVIDER`。A 和 B 可以用同一个适配器类，但不能复用同一个对象，也不能落入同一个无产品分区的数据空间。

## 4. 工作流、提示词与反思如何分工

- `Workflow`（工作流）：决定下一步回答还是提出工具调用；
- `PromptStrategy`（提示词策略）：把公司指令与不可信用户输入分开渲染；
- `ReflectionStrategy`（反思策略）：审查工作流的最终回答，可接受或改写，不能直接执行工具；
- `ExecutionRunner`（统一执行器）：解析并执行工具意图，产品不可替换。

例如：

- A 使用默认 ReAct 和 A 的系统提示词；
- B 使用固定步骤工作流且禁用反思；
- C 使用 ReAct，但最终答案再走单次模型反思。

三种产品可以在 ABC 组合中并存，因为策略从请求的准确产品绑定解析。不要在一个全局变量里根据产品字符串分支，也不要让 A 的工作流直接持有 B 的服务。

`request.conversation` 是服务器从持久转录重建的有界多轮历史，不是客户端提交内容。默认按用户隔离；部署方显式开启共享会话后，历史用户消息会保留可信主体归属。产品应读取该字段，不要另建一个不带公司/产品/会话所有者边界的全局聊天缓存。

自定义工作流必须实现：

```python
from suiteharness.execution import (
    PromptEnvelope,
    RunRequest,
    WorkflowDecision,
    WorkflowFrame,
)


class ProductAWorkflow:
    async def next(
        self,
        request: RunRequest,
        frame: WorkflowFrame,
        prompt: PromptEnvelope | None,
    ) -> WorkflowDecision:
        # 可以返回 final，也可以返回 tools；不能直接调用工具处理器。
        return WorkflowDecision.final(
            {
                "product": request.scope.product_id,
                "message": "product-a 已处理请求",
            },
            state={"step": frame.iteration},
        )
```

工作流状态、工具参数和最终输出都必须是 JSON 值，并受字节预算限制。

## 5. 画像、证据与知识

产品可绑定：

- `ProfileProvider`（用户画像提供器）：必选，产品私有；
- `EvidenceProvider`（原始证据提供器）：可选，原始文档/媒体的独立权限边界；
- `KnowledgeProvider`（知识提供器）：可选，授权必须先于检索和排序。

所有方法都接收当前 `RequestScope`，适配器必须用其中的 `tenant_id`、`product_id` 和主体建立物理数据边界。推荐数据库键形状：

```text
(tenant_id, product_id, local_subject_id, record_id)
```

反例：

```text
(email, record_id)                 # 缺公司和产品边界
(tenant_id, local_subject_id)      # A、B 可能串库
```

内置 `InMemoryProfileProvider`、`InMemoryCustomer360Provider` 和词法知识提供器用于契约测试与开发，不是公司生产存储。

## 6. 默认隔离与显式 Customer360

单产品 A 和多产品 AB/ABC 都默认：

```yaml
customer360:
  mode: isolated
  rules: []
```

只有确有业务依据时才改为策展式共享：

```yaml
customer360:
  mode: curated
  rules:
    - producer: product-a
      consumer: product-b
      predicates:
        - preference.language
    - producer: product-b
      consumer: product-c
      predicates:
        - company.industry
```

上例只允许两个单向字段流：

- A → B：语言偏好；
- B → C：公司行业。

它不允许 B → A，不允许 A → C，也不允许读取生产方私有库。运行时还需要实体别名、候选提交、策展决定、消费规则和来源链同时成立。

如果 A 与 B 都需要同一份只读公共知识，优先让部署方配置两个产品各自的知识适配器或明确只读共享数据源；不要借用户画像共享机制传播知识库。

## 7. 注册产品工具

产品工具在 `install`（安装）阶段通过窄化的 `ProductActivationContext` 注册。下面是只读工具：

```python
from pydantic import JsonValue

from suiteharness.execution import ToolCallContext, ToolEffect, ToolSpec
from suiteharness.runtime import ProductActivationContext


async def install_product_a(context: ProductActivationContext) -> None:
    async def lookup(
        call: ToolCallContext,
        arguments: dict[str, JsonValue],
    ) -> JsonValue:
        query = str(arguments["query"])
        # 实际实现还要把 call.scope 传给产品存储适配器。
        return {"query": query, "items": []}

    context.register_tool(
        ToolSpec(
            name="product-a.catalog.lookup",
            description="查询 product-a 的产品内目录",
            effects=frozenset({ToolEffect.READ}),
            required_capabilities=frozenset({"product-a.catalog.read"}),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 200}
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        lookup,
    )
```

再把安装器传入 `PreparedProduct(install=install_product_a)`。

工具开发要求：

- `effects` 必须准确声明读、写、破坏性和外部访问；
- 写工具用 `WRITE`，删除/覆盖不可恢复状态还要加 `DESTRUCTIVE`；
- 网络调用要加 `EXTERNAL`；
- `required_capabilities` 必须是产品描述能力的子集；
- JSON Schema 关闭额外字段并设置字符串、数组和数值边界；
- 处理器仍要使用 `call.scope` 做产品级数据过滤；
- 结果必须有界、可序列化且不包含密钥；
- 产品工具不要重写框架已有 `fs/grep/Bash/web_search/web_fetch`。

注册成功也不代表用户可调用。服务器还需在准确 `(channel_id, product_id)` 工具模板中加入别名，运行时才会解析为完整工具身份并签发短时授权。

## 8. 产品内部 Runtime Bundle

当一个产品内部有可选或复用组件时，使用 `BundleManifest`：

```python
from suiteharness.runtime import (
    BundleInstallContext,
    BundleManifest,
    BundleRequirement,
)


class ProductAKnowledgeBundle:
    manifest = BundleManifest(
        bundle_id="product-a-knowledge",
        version="1.0.0",
        harness_api=">=0.1,<0.2",
        requires=(
            BundleRequirement(
                bundle_id="product-a-storage",
                version=">=1,<2",
            ),
        ),
        provides=frozenset({"product-a.knowledge"}),
        capabilities=frozenset({"product-a.catalog.read"}),
    )

    async def install(self, context: BundleInstallContext) -> None:
        resource = build_product_a_knowledge_client(context.config)
        context.own("product-a-knowledge-client", resource.close)
```

产品描述把所需能力包列为根依赖，激活器把所有可用实现放入 `PreparedProduct.bundles`。内核自动解析闭包和安装顺序，不要在产品安装器里再次手工解析或安装。

`provides` 目前主要用于规划期冲突检查，不会自动写入 `ServiceBindings`。若运行时要通过服务键解析，激活器仍需把对应 provider 放进 `PreparedProduct.bindings`。

## 9. MCP 应放在哪里

MCP（Model Context Protocol，模型上下文协议）服务按产品在公司配置中声明，而不是由模型动态添加：

```yaml
mcp:
  servers_by_product:
    product-a:
      - server_id: product-a-docs
        enabled: true
        protocol_versions:
          - "2025-11-25"
        # transport 及其参数依据实际配置模型填写
```

具体字段请以 [配置示例](../config/suiteharness.example.yaml) 和 `suiteharness.config.models` 为准。服务器会把连接绑定到 `(tenant, product, server)`，把远程工具桥接到统一执行器。

远端声明的“只读”只是提示。MCP 工具默认按写 + 外部访问处理；只有部署管理员带理由的 `tool_security_overrides` 才能调整。飞书不会因此自动获得写权限。

发现工具也不等于渠道获权。管理员还必须在 `mcp.channel_access.web|feishu.<product>` 显式配置：`allow_servers` 选择一个服务当前发现的全部工具，`allow_tools` 按服务和远端原始工具名精确选择；同一服务不能同时使用两者，默认空即全部拒绝。产品代码不要生成或修改这份控制面白名单。运行签发短时授权时会再次核对准确工具身份和能力；飞书只能选择最终注册为只读的 MCP 工具。

产品测试至少要覆盖：

- MCP 服务不可被其他产品解析；
- 工具重名时身份和别名确定；
- 服务断开、取消、超时和重连；
- 远端返回超限或恶意内容；
- stdio 沙箱或 HTTPS 出口策略；
- 渠道默认拒绝、按服务/工具白名单、未发现工具和身份/能力漂移；
- 协议版本和实际能力协商。

## 10. 何时使用插件

Plugin（插件）适合部署时选择、独立制品校验和生命周期管理的扩展，例如可替换知识适配器或工具集合。普通产品源码内稳定组件可直接用 ProductActivator/Runtime Bundle，不必为了“看起来可插拔”全部包装成插件。

插件必须提供严格 `suiteharness-plugin.json`，声明：

- 插件标识、版本、Harness API；
- `trusted_in_process`、`isolated_worker` 或 `mcp` 信任模式；
- SHA-256 制品摘要和生产签名；
- 提供/依赖服务；
- product、memory、knowledge、workflow、reflection、prompt、tools、models、mcp、channels 贡献；
- 封闭权限集合和严格配置 Schema。

生产中的进程内插件必须精确允许且按服务器代码审查。隔离模式还需要部署方提供真实工作进程启动器；仅在清单写 `isolated_worker` 不会自动生成安全沙箱。

## 11. 把 A 扩展成 AB/ABC

产品代码不应知道“自己当前是否和哪些产品一起销售”。组合由公司配置决定：

```yaml
customer_bundle:
  products:
    - product_id: product-a
      version: "==1.0.0"
      config:
        system_prompt: "你是 product-a 的公司级 Agent。"
        knowledge_top_k: 5
    - product_id: product-b
      version: ">=2,<3"
      config: {}
    - product_id: product-c
      version: "==1.2.0"
      config: {}
```

扩展步骤：

1. 把 B/C 的静态描述加入受信产品目录；
2. 把产品选择加入 Customer Bundle；
3. 提供 B/C 激活器和各自画像实例；
4. 为各渠道增加准确的产品路由和工具模板；
5. 分别准备产品工作区、模型路由和 MCP；共享工作区只在确有文件协作需求时逐产品放开；
6. 只有确实需要时才增加单向策展共享规则；
7. 对 A、AB、ABC 分别运行组合、安全和回滚测试。

不要复制 A 的整个服务进程再把 B 逻辑塞入同一全局对象，也不要用组合名称拼接数据库表来代替准确作用域。

## 12. 激活示例

```python
from suiteharness.runtime import (
    CustomerBundleManifest,
    HarnessKernel,
    ProductCatalog,
    resolve_customer_bundle,
)


catalog = ProductCatalog([PRODUCT_A, PRODUCT_B])
manifest = CustomerBundleManifest.model_validate(customer_bundle_config)
plan = resolve_customer_bundle(
    manifest,
    catalog,
    harness_version="0.1.0",
)

activation = await kernel.activate_customer_bundle(
    tenant_id="company-a",
    plan=plan,
    activators=[
        product_a_activator,
        product_b_activator,
    ],
)
```

实际公司服务应优先使用 `FoundationRuntime.kernel`，因为它已连接内置工具、Docker/开发沙箱、模型路由、渠道策略、SQLite 会话/审计/运行状态和默认 Agent 策略。激活后还要调用服务器扩展宿主连接配置的插件/MCP，并组合渠道应用。

## 13. 必测契约

每个产品至少测试：

- 描述和配置：未知字段、错误版本、Harness API 不兼容；
- 隔离：A 的 scope 不能查询/写入 B 的画像、知识、工作区或工具；
- 共享工作区：未列出的产品不可见、`read_only` 产品不能写，飞书还需额外路径与工具授权；
- 复用：相同 provider 类的 A/B 实例仍使用独立物理命名空间；
- 工作流：只能返回 final/tools，不能绕过统一执行器；
- 工具：Schema、效果、能力、授权缺失、只读拒绝、审批和预算；
- 出口：A 允许的 Bash network、搜索 provider 或抓取 route 不能被 B/C 通过工具参数选择；
- 生命周期：prepare/install 失败全部回滚，关闭后不可运行；
- 幂等：重复外部消息不重复副作用，不确定运行失败关闭；
- Customer360：默认隔离、单向字段规则、审核、撤回和来源链；
- MCP/插件：产品归属、制品验证、权限上限、断线和回滚；
- 渠道：Web 写入审批，飞书只读/白名单写入/删除永禁；
- A、AB、ABC：组合顺序不改变解析结果，新增产品不污染已有产品。

测试时不要把进程内参考 provider 通过作为“生产数据库已安全”的证明。生产适配器需要额外做租户过滤、事务、并发、备份、恢复、迁移和权限测试。

## 14. 常见错误

- 在 Root/Tenant 放产品私有画像；
- A、B 使用同一个存活 provider 或同一个无产品分区的数据表；
- 把产品 capability 声明当成用户授权；
- 工作流直接执行 Python 函数、Shell 或 HTTP；
- 错报工具效果，尤其把外部写工具标为只读；
- 认为运维创建了一个出口网络就等于所有产品都能使用；
- 只配置飞书可写目录，却没有准确工具授权模板；
- 相信 MCP 远端注解自动降权；
- 未验证插件摘要/签名就导入入口；
- 在准备阶段发布外部可见副作用；
- 把本地开发沙箱当生产安全边界；
- 在产品代码内判断 A/AB/ABC 并跨产品取数据；
- 把 shared 工作区和 Customer360 策展共享混为同一个权限开关；
- 把用户消息、模型输出或网页文本拼入系统提示词而不分隔。

进一步了解底座模块见 [中文模块指南](module-guide.zh-CN.md)，上线装配见 [公司部署指南](deployment.zh-CN.md)，权限细节见 [安全模型](security-model.md)。
