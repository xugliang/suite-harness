# SuiteHarness 通用 Skill 能力与产品 A 考核 Skill 详细设计

日期：2026-10-19。状态：详细设计，尚未实施功能代码、执行测试或切换服务。

## 1. 目标与实施范围

核心原则：**框架提供 Skill 基础机制，产品提供 Skill 内容与业务规则。**

- SuiteHarness 提供受控发现、元数据目录、完整读取、请求内加载记录、工具依赖检查、上下文保留、诊断与兼容默认实现。
- 产品 A 保留全部考核 Skill、命令语义、审核员设置、题库访问、抽题、评分、抄送和消息发送实现。
- 产品 B 及其他未启用 Skill 的产品不需要补写抽象方法，不出现考核目录，不增加模型轮次。
- 保持已确认的岗位职能、员工工作画像方案；本次实施范围不顺带开发画像数据库或重写岗位提示词。
- 不新增一个 Skill 服务进程，不新增 Skill 数据库，不改变现有业务数据库结构。
- 不因 Skill 改造修改主机防火墙、Docker 网络、模型服务商、业务数据或企业权限名单。

本文的新增接口、字段、配置与目录均为拟定设计，不表示当前代码已经存在。

## 2. 已核实的当前实现

### 2.1 读取范围

- 框架服务器目录：`/opt/xj_ai_suanfa/xgl/suite-harness`。
- 产品服务器目录：`/opt/xj_ai_suanfa/xgl/suite-products`，本次读取 HEAD 为 `d3b1f77`。
- 产品 A 运行容器：`suite-products-a-1`；镜像标签为 `suite-products-a:conversation-clear-v1-20261014`。
- 已比对关键框架与考核文件，所检查文件在宿主目录与 A 运行容器中的 SHA-256 一致。该结论只覆盖已检查文件，不代表两个目录的所有文件都一致。
- 框架宿主目录当前没有 `.git`。后续提交前必须找到或恢复其受版本管理的规范工作副本，不能在其他仓库根目录误提交，也不能把部署目录冒充已验证的 Git 提交。
- 产品现有未跟踪目录 `.codex-backups/`、`.codex-work/` 和文件 `1` 不属于本次功能，不清理、不提交。

### 2.2 框架接口事实

| 位置 | 当前行为 | 本设计需要解决的问题 |
|---|---|---|
| `execution/models.py`：`ToolSpec` | 声明工具说明、效果、权限、输入 Schema | 尚无 `required_skills` |
| `execution/protocols.py`：`ToolCallContext` | 提供 scope、run_id、call_id、真实工具身份等 | 不提供可变运行状态，不应改成任意 handler 都能修改的加载集合 |
| `execution/runner.py`：`_RunState`、`_execute_intent()` | 框架掌握真实执行和本轮状态 | 应在这里掌握可信加载记录和执行前置检查 |
| `execution/runner.py`：`_execute_batch()` | 同批工具通过并发方式执行 | 同批次 read＋write 不能靠时序碰巧放行 |
| `tools/builtins.py` | 内置工具使用受保护身份注册 | Skill 读取应属于受保护内置工具 |
| `runtime/kernel.py`：`PreparedProduct` | 产品通过 bindings、install 等接入 | 可以增加可选技能 provider，不必修改旧产品必实现接口 |
| `agents/react.py`：`ReActWorkflow` | 从 observation 生成标准 tool message；恢复状态后不重新采用新 PromptEnvelope | 不能只在下一次 render 时拼接正文，然后误认为模型已经看到 |
| `models/context.py` | 对普通 TOOL 内容有截断处理 | Skill 规则不能被静默截断后仍视为有效 |
| `execution/runner.py` | 部分工具结果大于 16 KiB 时转为结果引用 | 读取到引用不等于已向模型提供完整 Skill |

目前没有通用 `skills/` 实现，也没有本轮可信 `loaded_skills` 机制。

### 2.3 产品 A 接入事实

- `activator.py` 当前向普通问答的 system 拼接完整考核一级 Skill。
- `assessment_skills.py` 只负责产品内部固定目录读取和 root＋child 拼接；当前文件没有元数据 frontmatter。
- `assessment_prompt.py` 为明确事务构造独立提示词，不继承普通聊天历史。
- 自然语言下发可能直接走普通 ReAct，目前没有强制先读 Skill 的执行检查。
- 部分明确自然作答会先被产品转换为 submit，再查询本人的当前任务。
- `/确认抄送`、`/结束考核` 有程序直达路径；它们不是需要模型自由决定的操作。
- `assessment_reply.py` 将本轮匹配的 tool call/result 交给独立回执模型，关闭工具，并隔离旧聊天。

### 2.4 OpenHarness 的参考点

参考代码来自 7.22 的 `/opt/xj_ai_suanfa/xgl/xuanbot/src/openharness`：

- `prompts/context.py::_build_skills_section()`：常驻名称和描述，匹配后通过 skill 工具读取。
- `tools/skill_tool.py`：按登记名称返回技能正文，读取本身是只读行为。
- `commands/registry.py`：明确用户命令可以直接展开对应技能。
- `skills/types.py`：区分元数据、正文、用户调用和模型调用条件。

借鉴渐进加载方式，不照搬其个人工作目录扫描、编程助手规则或权限策略。

## 3. 分层与职责

```text
产品 A 仓库                                  SuiteHarness 仓库
考核 Skill / 回执文件 ──显式登记──► 通用文件读取与注册表
考核命令及路由       ──声明预加载─► 通用预加载入口
考核工具            ──声明依赖──► 执行前置检查
考核身份与状态检查   ◄─实际调用─── 可信工具执行器
                                               │
普通请求 ─► 简短技能目录 ─► 模型选 Skill ─► skill.read
                                               │
                             完整正文＋本轮加载记录
                                               │
                         模型生成业务参数 ─► 原业务工具
```

### 3.1 框架必须具备

1. 可选 `SkillProvider` ABC 与默认空实现、文件实现。
2. 只暴露当前作用域可见 Skill 的短目录。
3. 受保护内置工具 `suiteharness.skill.read`。
4. 单轮、单产品、单操作者隔离的可信加载记录。
5. `ToolSpec.required_skills` 执行前置条件。
6. 明确命令的可选 `preload_skills`。
7. Skill 完整性、内容上限、依赖展开、上下文保留及失效处理。
8. 通用日志、字符统计、错误反馈和回归测试。

### 3.2 只能放在产品 A

考核题目数量、人员限制、产品线、题库地址、审核员名单、员工解析、任务状态、评分方法、命令前缀、抄送确认、消息格式、发信幂等和业务重试。

框架源码及通用默认提示词中不得出现考核命令、员工姓名、公司产品名或业务数据库表名。

## 4. 最小外部接口

### 4.1 SkillProvider 使用 ABC

拟定接口示意；实现时使用项目现有类型风格和真实 RequestScope，不另造身份体系：

```python
class SkillProvider(ABC):
    @abstractmethod
    async def list_skills(self, access: SkillAccess) -> tuple[SkillDescriptor, ...]:
        """只返回当前产品与访问条件下可见的元数据。"""

    @abstractmethod
    async def read_skill(
        self, access: SkillAccess, skill_id: str, *, resource_id: str = "instructions"
    ) -> SkillDocument:
        """按登记 ID 返回完整静态内容；不是通用文件读取。"""

    def close(self) -> None:
        return None
```

- 以 `SKILL_PROVIDER: ServiceKey[SkillProvider]` 在产品 scope 绑定。
- 提供 `EmptySkillProvider`，目录为空，读取返回不可用；未启用产品使用该默认行为。
- 提供 `FilesystemSkillProvider`，大多数产品只需声明目录和包信息，无须自己实现文件加载器。
- 不给现有 `ProductActivator`、`PromptStrategy` 增加必须实现的方法。
- 后续可选能力优先提供基类默认实现或独立扩展接口，不扩大既有必实现方法集合。

### 4.2 拟定代码落点

```text
src/suiteharness/skills/
├── models.py          元数据、文档、错误与读取条件
├── protocols.py       SkillProvider ABC、空默认实现
├── filesystem.py      受控解析、路径检查和静态内容快照
├── registry.py        命名空间、目录、依赖展开和版本一致性
├── runtime.py         本轮账本、预加载、上下文桥接与阶段隔离
└── services.py        产品作用域 SKILL_PROVIDER
```

文件划分按实施时实际规模收敛，不为每个小函数单建一个模块。受保护工具接入 `tools/builtins.py`；真实门禁接入 `execution/runner.py`；工具/命令可选字段放在现有定义文件；标准 observation 和上下文保留接入 `agents/react.py`、模型网关和 `models/context.py`。这几处接入只处理通用机制，不出现考核判断。

### 4.3 SkillAccess：可信访问上下文

包括现有 `RequestScope`、当前 run 身份、调用来源，以及由框架命令解析得到的只读命令信息。调用来源区分 `model`、`command`、`workflow`。

- 所有身份、产品范围和命令条件由服务器填入，模型不可传入。
- 模型不能提供另一位员工的 user_id，也不能通过 `origin=command` 伪造明确命令。
- Skill 可见性只控制能否发现或读取说明，不替代业务工具授权。
- 产品需要限制维护类说明时，可注入读取策略；不能由 Markdown 中的 `trusted: true` 自行授权。

### 4.4 数据模型

| 模型 | 主要字段 | 说明 |
|---|---|---|
| `SkillDescriptor` | `skill_id`、`name`、可选 display_name、`description`、`version`、`source_sha256`、调用方式 | 用于目录，不包含正文或真实业务数据 |
| `SkillDocument` | descriptor、`resource_id`、正文、受控依赖和来源标识 | 一次读取的完整结果 |
| `SkillLoadRecord` | skill_id、版本、合成正文摘要、来源、本轮作用域 | 由框架生成，不能作为模型工具入参 |
| `SkillRuntime` | 已准备正文、实际上下文状态、本轮加载账本 | 内部实现；不对产品暴露可随意修改的集合 |

字段约束：

- ID 使用稳定命名空间，例如 `feature-assistant.assessment.assign`；最大 128 个字符。
- `name` 是技能包内的稳定名称，例如 `assessment.assign`；中文标题放可选 display_name。工具参数使用完整 skill_id。
- 描述建议不超过 256 个字符，必须表达实际触发条件，不展开操作正文。
- `version` 为维护者版本；`source_sha256` 标识文件原始字节，`instructions_sha256` 标识规范化并展开依赖后的完整说明。版本相同但文件变更仍视为不同内容。
- SHA-256 不等于签名，更不代表该文档有权限。
- ID 是注册键，不是路径；不根据 ID 拼出任意磁盘路径。
- 空正文、未知字段及无效元数据在启用时直接报告，不静默跳过必要规则。

## 5. Skill 文件与受控资源

### 5.1 产品目录

保留产品 A 的现有目录：

```text
products/feature_assistant/assessment_skills/
├── SKILL.md
├── assign/SKILL.md
├── assign/REPLY.md
├── answer/SKILL.md
├── answer/REPLY.md
├── results/SKILL.md
├── authoring/SKILL.md
├── authoring/REPLY.md
└── close/SKILL.md
```

一级正文保存公共规约；二级正文保存场景步骤。目录只用元数据，不能把一级正文重新常驻。

### 5.2 文件示例

```markdown
---
name: assessment.assign
display_name: 下发考核
description: 给指定员工下发考核题，适用于自然语言请求及 /考核。
version: 1.0.0
includes:
  - assessment
---

# 下发考核

根据当前请求取得接收人员、题数和产品线。
缺少必要信息时澄清，不自行编题或猜测人员。

调用下发工具，由后台核验人员并从题库抽题。
参数范围以工具 Schema 为准，不在本文件重复维护人数等限制。

依据本次实际工具结果反馈。完成后，按登记的回执资源整理结果。
```

说明：`includes` 为本设计新增的受控依赖元数据，不声称 OpenHarness 原样支持。归属产品、真实读取权限和资源路径由产品登记信息决定，不接受文件自报跨产品权限。

名称解析规则唯一且在启动期固定：产品命名空间由激活器绑定为 `feature-assistant`，本地 name `assessment.assign` 解析为 `feature-assistant.assessment.assign`；root 的 name `assessment` 解析为 `feature-assistant.assessment`。includes 中的 `assessment` 只在同一登记包内解析为该 root，不根据目录名猜测，不在其他产品中搜索同名对象。对外读取工具只接受完整 ID，别名不进入工具入参。旧目录 assign/answer 等可以保留，文件夹名称不决定身份。

### 5.3 公共规约和引用

- 加载 assign 时，由注册表一次展开公共规约＋assign，避免模型先读一级、再读二级造成两次往返。
- 依赖必须在同一受信任技能包或明确允许的注册集合中，禁止循环、未定义依赖和无限递归。
- 依赖展开逐项核验访问策略；“同包”不意味着可以借公共 Skill 读取该员工不可访问的受限说明。
- `REPLY.md` 登记为内部 `reply` 资源；初版不向模型提供自由枚举、读取所有资源的工具。
- 回执工作流通过内部调用读取 `resource_id="reply"`，仍使用同一 provider、安全检查和版本。
- 初版模型读取工具只读取 `instructions`。将来确有长参考文档需要时，再开放受控 resource_id，不扩展为 read_file。
- Markdown 链接不自动抓取；引用路径不自动扩大允许目录。
- 文件中不保存实际题目、标准答案、个人任务、员工名单、令牌或密码。

### 5.4 调用方式

- 支持 OpenHarness 常用的 `user-invocable`、`disable-model-invocation` 元数据语义，但它们仅决定发现和调用方式。
- assign、answer 保持模型可选择，保留无斜杠能力。
- authoring、close 可以只由正式命令进入；results 的普通查询和受限抄送仍由产品路由区分。
- 读取结果说明不代表获得抄送权限；不能用一个元数据开关代替命令校验。
- 框架不从文件名自动生成所有产品的业务命令。产品显式登记现有命令，避免意外扩大入口。

## 6. 内置读取工具

工具：`suiteharness.skill.read`。效果：READ。由 `BuiltinToolInstaller` 使用受保护身份注册。

读取继续走现有工具 grant/能力检查，不因“只读”跳过授权。产品启用 Skill 时由安装阶段登记该内置工具的访问能力；不能因为目录存在就向所有产品自动授予。实现须检查现有能力词表并增加必要的通用读取能力声明及默认禁用测试，不借用 Bash 或任意宿主文件权限来放行。

初版模型输入 Schema：

```json
{
  "type": "object",
  "properties": {
    "skill_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 128,
      "description": "当前可用技能目录中的完整 Skill ID，不是文件路径。"
    }
  },
  "required": ["skill_id"],
  "additionalProperties": false
}
```

不得接受 product_id、user_id、路径、URL、权限角色或“已加载”标志。

成功结果示意：

```json
{
  "skill_id": "feature-assistant.assessment.assign",
  "version": "1.0.0",
  "instructions_sha256": "<实际合成正文摘要>",
  "instructions": "<完整公共规约与下发说明>"
}
```

- 上述返回字段用于模型理解与诊断；JSON 自身不是加载凭证。
- 只有受保护工具的真实身份、当前 provider 和执行结果共同产生可信记录。
- 产品不能用同名或同格式普通工具返回伪造加载成功。
- 结果进入标准 assistant tool_call → tool message，不伪造用户发言，不新增模型厂商不支持的 role。
- 只有受控 Skill 加载器返回的说明可作为本任务指导；普通文档、题干和检索结果仍为数据。Skill 也不能覆盖系统安全规则。
- 未启用产品不发布该工具、不追加目录、不增加模型调用。

## 7. 目录生成与提示词装配

### 7.1 常驻部分

框架统一生成“可用技能”短目录与使用说明；产品 A 只补充一条短行为边界：不得代答或提前提供考核答案。

目录仅包含当前产品、当前访问条件可见且可由模型主动选择的技能元数据。命令专用说明可以出现在用户帮助中，不必全部占用普通模型上下文。

短目录示意：

```text
请求匹配下列技能时，先调用 suiteharness.skill.read 读取说明，再按说明执行。
- feature-assistant.assessment.assign：给员工下发考核题，支持自然语言。
- feature-assistant.assessment.answer：提交本人答案或处理待答任务。
- feature-assistant.assessment.results：查询考核结果；额外抄送仍须正式命令与确认。
```

不能把 root 正文藏在另一个字符串里继续常驻。实际字符量由日志统计，不以硬凑到某个字数验收。

### 7.2 自然语言路径

1. 普通模型看到短目录。
2. 模型选择并调用 skill.read。
3. 框架返回完整说明，记录可信准备状态。
4. 说明完整进入下一次实际模型请求。
5. 模型产生业务工具参数。
6. runner 检查 Skill 前置条件及原权限，执行原工具。
7. 按当前场景输出或进入独立回执工作流。

无明确考核任务时，不读取 current_task，不加载全文。

### 7.3 明确命令预加载

`CommandSpec` 增加可选 `preload_skills=()`，默认空。

- 产品 A 为需要模型理解参数的 `/考核`、`/回答`、`/抄送`、题库维护命令声明相应 Skill。
- runner 固定真实命令路由后，通过相同 provider 预加载；正文在首次模型调用前进入受控的任务提示词。
- 产品 A 仍通过自己的 command_prompt 保留“只含本条输入”的事务隔离，不重新加入普通会话历史和画像。
- 预加载不能只拼文字而不登记，也不能只登记而不给模型正文。
- `/确认抄送`、`/结束考核` 保持程序直达，不新增一次 Skill 模型选择或额外 LLM 请求。这类路径不是模型行为，原命令和业务状态校验就是执行依据。
- 确定的自然作答进入 submit 时，由产品通过通用内部入口预加载 answer；原始消息不是斜杠命令，不伪造 command 来源。

## 8. required_skills 与本轮可信账本

### 8.1 工具声明

`ToolSpec` 增加默认空元组 `required_skills`。产品 A 声明示例：

该字段属于宿主执行元数据，不混进模型厂商的 function 参数或 JSON Schema。给模型的工具仍只有标准名称、说明和 input_schema，避免破坏千问等兼容接口。

| 业务工具 | 需要的 Skill |
|---|---|
| assessment.assign | assessment.assign |
| assessment.current_task、assessment.submit | assessment.answer |
| assessment.history、assessment.prepare | assessment.results |
| assessment.questions.add | assessment.authoring |
| assessment.confirm、assessment.close | 保持原程序直达；不强制模型加载 |

具体注册时使用完整 Skill ID。缺少依赖对应定义的产品应在启用阶段失败，而不是上线后首次发信才发现。

启用校验使用登记技能包的内部静态清单，检查 ID、依赖与资源完整性，不使用面向员工且可能过滤的 list_skills()。文件 provider 在编译登记包时生成该清单；将来外部 provider 也必须由管理员显式登记对应包清单。请求阶段再按真实 SkillAccess 生成可见目录和检查读取条件。启动检查不构造虚假高权限员工 scope，内部完整清单不发给模型。

### 8.2 检查位置与顺序

由可信 `ExecutionRunner` 管理，不用产品全局 set，不从历史文本或模型参数推断。

1. 先固定真实工具身份、请求作用域及命令条件。
2. 按现有顺序完成访问、权限与参数检查，避免未授权者通过错误信息枚举隐藏 Skill。
3. 检查本批开始时的有效 Skill 状态及对应正文是否完整可用。
4. 满足后才进入业务 handler；Skill 检查绝不跳过原业务授权。

缺失时返回可恢复错误，例如：

```json
{
  "code": "skill_required",
  "message": "执行该工具前须先读取指定技能。本次未执行业务动作。",
  "required_skills": ["feature-assistant.assessment.assign"],
  "next_action": "调用 suiteharness.skill.read 后重新提交业务工具调用。"
}
```

未授权读取则对外返回不可用，不泄露其他产品或受限技能存在性；内部诊断保留具体拒绝原因。

### 8.3 原子性与同批并发

当前批次并发执行，因此明确规定：**依赖判断使用批次开始时的加载快照。**

- 初次同批调用 read(assign)＋assign：read 可以成功，assign 返回 skill_required，发送次数为 0。
- 下一次模型工具调用才可使用刚加载的 Skill。
- 已在前一轮完整加载且当前有效的 Skill 不受此限制。
- 不依赖 asyncio 调度顺序，不因为批次里“包含一个读取工具”就放行。

### 8.4 本轮状态的生命周期

账本绑定 product scope、当前 principal、运行身份、Skill ID、版本和正文摘要；它不是可跨轮使用的权限票据。

- 产品实例可缓存静态文件；员工 A 与 B 的加载状态不共享。
- 新用户消息创建新的任务加载状态；聊天记录里的旧 skill.read 不算本轮加载。
- 子 Agent 自己建立加载状态，在其实际授权范围内重新读取；不能沿用父 Agent 的标志扩大范围。
- `/clear` 后不存在可通过历史恢复的跨 run 标记；磁盘静态 Skill 文件不属于个人记忆，不删除。
- WorkflowFrame 可提供只读加载元数据用于观察，但产品可替换的 workflow_state 不是权威账本。

### 8.5 运行状态到工作流／模型网关的桥接

当前模型调用发生在 Workflow 内部，而不是由 runner 接到“调用模型”决策后统一发起。因此仅增加 ToolSpec 字段不够，必须落实这条通用桥接：

1. runner 为每次 run 建立独立 SkillRuntime，并以执行上下文绑定到当前异步调用链。
2. 使用受控的任务局部上下文（可采用 ContextVar），在进入 run 时绑定，finally 中恢复；它保存 runner 所有状态的受控访问引用，不是进程全局的可变 loaded 集合。
3. 内置读取工具通过该执行上下文提交真实读取结果；不能让产品 handler 直接修改 `_RunState`。
4. 为命令预加载和自定义 Workflow 提供通用内部 `preload_skill(...)` 入口：入口接收登记 ID，以当前真实 scope 检查和读取，只产生 prepared 状态，不能直接声明 active。
5. 产品 A 的确定自然作答路由在本轮首次模型调用前，通过此入口请求 answer Skill，不伪造斜杠命令。
6. 共享模型网关／上下文准备层取得只读技能附件，在完整装配后登记 actual-request 状态。至少覆盖 stream 与非流式 generate，不能只适配默认 ReAct 而漏掉产品 A 的封装网关。
7. 对自定义网关无法提供完整性证据的路径，不允许把 Skill 依赖标成 active；启用时或首次调用前给出明确的不支持错误。

模型请求的 metadata、用户输入或产品可替换 JSON 不能直接提供该上下文引用。ContextVar 的异步子任务继承不等于获得额外产品权限；读取仍按真实 scope 检查。进程内可信产品代码本来具有宿主授予的权限，本设计不把 Python 私有属性当作对恶意宿主代码的隔离沙箱。

静态内容可在启用时缓存；当前人物、任务、模型请求和 active 状态不可进入共享缓存。

## 9. 正文进入模型、压缩与恢复

### 9.1 两阶段状态，避免“读过但没看到”

内部区分：

1. `prepared`：静态正文完整读出、依赖和大小校验完成。
2. `active`：对应正文完整进入该任务实际提交给模型的上下文。

依赖检查不能只看 prepared 布尔值。模型相关业务动作要求本任务的完整说明处于有效上下文状态。由框架 observation 装配及模型请求准备链路登记，不接受模型声称已经阅读。

active 不是“本 run 曾经看过”的永久标志，必须关联 **task_stage_id＋实际 model_request_id＋完整正文摘要集合**。工具依赖检查只接受产生本次工具意图的真实模型请求的完整性记录，不接受同一 run 其他请求、摘要、反思、回执或失败请求的 active 记录。

共享网关为真实请求生成内部请求标识，在完整请求经过 ContextManager 后绑定相关摘要，并将返回的工具 call_id 关联到该请求。标准 ReAct 与产品自定义网关必须保留这条关联。压缩或重装配后重新确认；如果无法关联调用来源或无法证明正文完整，则停止依赖操作，而不是从模型提供的 metadata 补造记录。现有纯程序直达命令没有模型工具意图，也不新增这种前置要求。

对于普通 ReAct，必须通过真实 observation 或框架受控装配进入消息。不能依靠后续 PromptStrategy.render，因为当前 ReAct 已恢复的 messages 不会自动采用它。

### 9.2 大小预算

建议首版保守默认值，实施时用真实回归结果校准：

| 限制 | 拟定默认值 |
|---|---:|
| 单个 YAML 元数据头 | 4 KiB |
| 单次完整合成 instructions 输出 | 12 KiB，含公共规约及结果封装 |
| 本轮有效 Skill 正文总量 | 48 KiB |
| 本轮不同 Skill 数量 | 8 |
| 依赖最大展开深度 | 4 |

字节按 UTF-8 计算，不与中文字符数或精确 token 数混用。12 KiB 低于当前 16 KiB 转结果引用条件，但仍必须检查实际序列化后的完整输出。

- 超限时明确失败，要求维护者拆分，不截半本 Skill 然后标记成功。
- 指令正文不能分页后以第一页作为有效加载。
- 读取、模型请求、工作流状态和原总执行预算仍全部生效，不因 Skill 获得无限额度。

### 9.3 受控保留

- 为 Skill 目录和正文增加内部字符分类及来源标识，例如 `skill_catalog`、`skill_instructions`；分类只用于诊断，不独自决定信任。
- ContextManager 对经过真实加载登记的活动 Skill 保留完整正文，或从受信任正文缓存重新物化。
- 不能对这些正文沿用普通工具文本的静默截断逻辑，也不能只留下结果引用。
- 保留 tool message 时同时保留合法的 tool_call/result 配对，不能制造孤立工具消息。
- 普通工具或产品返回 JSON 自报“skill”不能获得保留待遇。
- 首版优先保留完整的最小调用结果组，避免复杂的角色迁移；若预算仍不足，返回明确上下文容量错误，不能悄悄丢规则继续执行。
- 不把所有历史 Skill 永久钉在上下文中；只保留当前任务实际启用且受总量限制的内容。

模型是否真正理解规则不能由程序证明；本机制保证的是规则已完整提供及执行前置条件，业务安全仍依赖原权限和状态校验。

### 9.4 独立回执不能被通用装配重新污染

共享网关不能简单给每一次模型请求追加本 run 读过的所有 Skill。

- 通用内部装配支持独立任务阶段：继承或不继承当前技能附件由受信任 Workflow 明确选择。
- 产品 A 的回执阶段使用不继承的局部上下文，只加载本次内部 reply 资源及匹配 call_id 的真实结果。
- 回执不自动带入考核一级正文、其他子 Skill、普通聊天、员工画像或历史结果。
- 此阶段仍为 `tools=()`、`tool_choice=none`；不能因为通用 Skill 工具存在而重新开放工具。
- 回执临时上下文退出后不修改父任务账本，不将结果接收者误当作新的操作者。
- 这一隔离通过框架通用上下文能力支持，但哪些阶段、需要哪些 reply 文件由产品决定。

明确命令的首轮提示词使用相同的受控附件装配，按 ID 和实际摘要去重，避免 command_prompt 与共享网关各拼接一遍正文。去重不以模型自报的文本标记作为依据。

### 9.5 中断与恢复

当前会话恢复不等于已经具备任意内层工具步骤的完整续跑，本次不借 Skill 改造宣称实现通用任意点恢复。

首版安全语义：

- 同一存活 run 内保持版本固定，不在执行中热替换 Skill。
- 跨进程或工作流恢复时，旧消息和 checkpoint 中的 Skill ID 不直接恢复为有效加载凭证。
- 根据可信身份和当前目标任务重新检查可见性，通过同一加载器重建正文并重新提供给模型。
- 版本或内容变化时使旧记录失效，重新规划后才能生成新的业务调用，不直接用旧参数继续写操作。
- 不能安全重建时停止依赖动作并报告；不为了“恢复”重放已发送或结果未知的副作用。
- Skill 读取是只读操作，可安全重新执行；业务发信、抄送和评分仍由既有幂等与恢复机制控制。
- 不将模型可控制的 workflow_state 字段当成可信账本，不必为首版新增业务数据库表。

## 10. 文件安全、缓存和版本

### 10.1 显式目录优先

- 企业部署只扫描配置或产品登记的受信任目录，不自动遍历员工 home、当前目录祖先或知识库附件。
- 同一 Skill ID 重复注册默认报错，不按照扫描顺序静默覆盖。
- 第三方 Skill 初版必须经管理员审核、显式登记，不允许运行时联网安装。
- 路径解析后必须仍在允许根目录；拒绝绝对资源路径、上级跳转、符号链接逃逸、非普通文件及异常文件大小。
- 文件读取应校验打开对象，避免只检查路径后再打开产生检查与读取不一致。
- Markdown 是静态文字，不执行脚本、命令替换、Python、YAML 自定义标签或环境变量展开。
- 元数据采用受限安全解析，限制别名、重复关键字和多文档等歧义输入。

### 10.2 版本与缓存

- 启用产品时解析并校验技能包，生成不可变目录与受控内容缓存。
- 缓存键至少包括产品归属、登记根目录和包摘要；只缓存静态说明，不缓存个人任务、答案或会话。
- 文件原始 UTF-8 字节生成 source hash；展开依赖后的规范内容生成合成 hash，诊断区分两者。
- 初版不做执行中热更新。文件变化通过校验和产品重新启用／正常发布生效，运行中的任务使用既定版本。
- 读取发现文件与已登记摘要不一致时停止使用，不静默混合新旧文件。

## 11. 配置、插件与兼容

### 11.1 通用配置建议

通过框架现有配置模型增加可选 skills 段；具体归属遵循现有配置层级，不绕开严格 Schema：

```yaml
skills:
  enabled: false
  max_instruction_bytes: 12288
  max_active_bytes: 49152
  max_active_skills: 8
  max_dependency_depth: 4
```

产品 A 在自身配置和激活登记中启用技能包并指定产品内目录。底座不写死 `/products/feature_assistant/assessment_skills`。配置是部署上限，不授予路径和工具权限。

### 11.2 插件接入

- 首版产品通过现有 `PreparedProduct.bindings` 绑定 SKILL_PROVIDER，工具安装复用现有作用域机制。
- 如需从现有插件服务贡献接入，再把 skills provider 纳入通用服务白名单；不得绕过插件现有信任与版本校验。
- 本次不新增一个能由 Markdown 注册可执行工具、执行任意脚本的插件协议。
- 声明式第三方技能包分发、市场、自动升级不在首版范围。

### 11.3 兼容保证

- `required_skills=()`、`preload_skills=()` 等新增字段均有默认值。
- 旧产品不安装 provider 时行为不变；产品 B 不展示 A 的考核元数据。
- 不向已有产品 ABC 增加新的必实现抽象方法。
- 旧检查点没有 Skill 状态时按未加载处理，不将旧文本升级为可信状态。
- 新产品配置要求 Skill 能力但底座不支持时启动即报明确版本不匹配，不允许半启用。
- 未配置 Skill、没有工具依赖、没有预加载时，新增路径为 no-op，不增加模型请求或扫描所有文件。
- 现有模型使用标准 tools/JSON Schema/tool message；不依赖厂商支持最终回答 response_schema。

## 12. 产品 A 的迁移清单

| 文件或目录 | 改动 | 保持不变的内容 |
|---|---|---|
| `assessment_skills/**` | 元数据、公共规约、场景步骤和回执分工 | 所有内容仍在产品仓 |
| `assessment_skills.py` | 改为产品登记与 route→skill 映射；读取委托框架 | 不自建第二套通用加载器 |
| `activator.py` | 绑定文件 provider、生成短目录；去掉常驻 root 全文 | 其他产品能力、配置和工具 |
| `assessment_prompt.py` | 使用预加载的可信公共＋子 Skill | 仅本条输入的事务隔离 |
| `assessment_workflow.py` | 接入通用预加载与账本；兼容自然作答路由 | close/confirm 直达、任务状态与工具阶段控制 |
| `assessment_tools.py` | 声明 required_skills | 员工核验、答案安全投影、状态与评分逻辑 |
| `assessment_report_tools.py` | 查询/预览声明依赖 | 原抄送前缀、草稿绑定和确认要求 |
| `assessment_authoring.py` | 维护工具声明依赖 | 审核员、原始命令和证据检查 |
| `assessment_reply.py` | 从统一 provider 读取内部 reply 资源 | 本轮 tool exchange、接收者隔离、关闭工具 |
| `authoring_prompt.py` | 移除导入时固定全文常量，改用版本对应资源 | 出题阶段及回执内容 |
| `authoring_context.py` | 只调整接入，不重写阶段控制 | 源码/知识证据策略、预算和写入阶段 |

不修改抽题算法、考核业务数据库、员工目录、题库工作表、评分规则和发信重试次数。人数和参数限制只从现有 Schema/配置取得，防止重新出现 20/50 多处不一致。

## 13. 错误反馈与日志

### 13.1 错误分类

| 错误 | 行为 |
|---|---|
| `skill_required` | 告知先读取哪个可见 Skill；本次无业务动作 |
| `skill_not_available` | 未找到或不可见；对外不泄露隐藏对象 |
| `skill_invalid` | 配置、元数据、依赖或正文非法；管理员修复 |
| `skill_content_changed` | 版本/摘要不一致，停止依赖操作 |
| `skill_budget_exceeded` | 正文或依赖超限；不返回残缺说明 |
| `skill_context_unavailable` | 无法保证完整进入模型上下文，停止依赖操作 |
| `skill_read_failed` | 实际读取失败；根据是否短暂故障决定是否可重试 |

错误进入标准 tool observation 反馈模型，避免只在后台日志可见。

- 参数、缺失文件、权限错误不原样盲重试。
- skill_required 是准备条件未满足，不计入实际发信次数；仍计入本轮总工具预算，避免无限循环。
- 原业务外部调用“首次＋最多两次重试”保持，不把一次 read 成功理解为发信成功。
- 读取或上下文失败不能促使模型自行编题、伪造回执或绕用其他发信工具。

### 13.2 观测字段

关联现有 run_id、request_id、product_id、scope 和工具 call_id，记录：

- 目录技能数量及字符数；
- Skill ID、版本、source/合成摘要、读取来源与耗时；
- prepared/active 状态及本轮读取次数；
- 正文字符数、UTF-8 字节数、依赖数量；
- 业务工具依赖检查结果及拒绝原因；
- 命令预加载、按需读取、回执加载分别标记；
- 是否在压缩中保留、重新提供或失败。

完整上下文仍遵守现有开发观测配置。框架不依赖 Phoenix 在线；日志和事件本身即可定位错误，Phoenix 只是可选展示端。

## 14. 测试设计

### 14.1 框架单元与契约测试

1. 空 provider 和未启用产品完全兼容。
2. 元数据、缺失正文、未知字段、重复 ID、依赖循环和大小上限。
3. 任意路径、跨产品 ID、符号链接逃逸、伪造 origin 和错误 Schema。
4. 读取成功与失败的账本状态；普通工具伪造返回值不能标记 active。
5. 同批 read＋write 的固定拒绝行为，不受调度快慢影响。
6. 两个员工、两个产品、子 Agent 和新 run 之间的隔离。
7. tool_call/result 配对与模型适配器兼容。
8. 工具结果大于内联阈值、ContextManager 截断、上下文紧张时仍完整或明确失败。
9. 旧检查点、变更版本、取消/恢复、clear 不产生历史加载绕过。
10. 未知/不可见技能对外错误的不可枚举性。

### 14.2 产品 A 无真实外发的端到端测试

使用脚本化假模型、假飞书适配器、临时 SQLite，走真实激活→提示词→工作流→执行器→业务工具→数据库→回执链路。不是只检查字符串。

| 场景 | 必须断言 |
|---|---|
| 普通产品问答 | 没有 root/child 全文，没有 current_task 查询 |
| `/考核` | 预载正确 Skill，实际发送一次，批次与接收者正确 |
| 无斜杠给员工发题 | 读取 Skill 后才能下发；跳过读取时零发送 |
| 同批读取与下发 | 下发被阻止，下一轮才可执行 |
| 人名无效或无权限 | 整次不下发，不删人重试、不自行编题 |
| “我的考核答案是 1.A 2.B” | 按作答处理，不误拦成索要答案 |
| 多轮部分作答 | 只确认真实保存和缺答，不推荐选项或补全答案 |
| 完成后或批次结束后回答 | 不回退旧任务，不重复保存、评分或通知 |
| 索要题库、答案、代答 | 不返回题库答案，不从其他工具旁路泄露 |
| 两人先后答题 | 收件人、姓名、任务和回执不串用 |
| `/抄送` | 仅预览本人有权查询的指定批次，不直接发送 |
| 普通“确认” | 即使加载 results 也不能发送 |
| `/确认抄送` | 程序直达，精确确认原草稿，最多发送一次 |
| `/结束考核` | 程序直达，原权限与状态转换不变 |
| 非审核员维护题库 | 即使能读说明也无权写入 |
| 出题证据与写入阶段 | 原证据、知识兜底与阶段工具控制保持 |
| 回执模型失败 | 不重执行业务写操作，不误报结果 |
| 读取异常、压缩、版本变化 | 无依赖动作，错误给模型及日志 |
| 不启用 Skill 的产品 B | 原启动、登录、问答流程不受影响 |

选择少量线上模型仅做提示词选择和标准工具协议冒烟，不向真实员工发送测试消息；不能用假模型测试通过宣称真实模型一定正确理解所有自然语言。

### 14.3 验收门槛

- 框架不含任何考核业务正文；产品 A 不再复制通用文件读取实现。
- 普通问答只有短目录；真实工具调用前有完整的对应说明。
- 未加载、越权、同批竞态、跨人和跨产品用例均不能产生受限副作用。
- 既有考核回归与新增端到端链路通过。
- 模型请求中可验证正文完整，不能仅凭服务器“读文件成功”日志验收。
- 版本、加载阶段、拒绝原因和各部分字符量可定位。

## 15. 开发与发布步骤

### P0：固定基线

记录规范代码路径、Git 状态、当前镜像、配置引用、现有测试结果和回滚镜像。框架目录非 Git 问题在提交前明确解决；不顺便清理旧目录或业务数据。

### P1：底座只读能力

实现模型、ABC、空/文件 provider、目录、受保护读取工具、路径与内容校验。先用独立最小示例验证，不启用产品 A 新路径。

### P2：底座执行与上下文完整性

加入 required_skills、命令预加载、可信账本、同批门禁、上下文保留与恢复安全语义。完成框架契约测试。

### P3：产品 A 迁移

整理现有文件，登记技能包及依赖，移除常驻全文，接入命令预载、自然语言读取及独立回执。保持原业务数据库和发送逻辑。

### P4：完整回归与独立审查

执行既有与新增考核端到端测试、产品 B 兼容测试、模型协议冒烟。独立检查范围、并发、压缩、恢复和授权设计是否在代码中真正落实。

### P5：正常应用发布

只构建与替换本项目应用镜像，不修改服务器系统配置。先确认新镜像中的框架与产品源码一致，再切换 A；核对启动日志、目录和一次不外发的读取链路。保留原镜像及配置作为回滚路径。

回滚必须成对回退框架与产品 A 镜像，不能把“已声明 Skill 依赖的新产品”放回“没有 Skill 门禁的旧底座”继续运行。本次无业务表迁移，因此回滚不需要转换考核数据；仍不得重放已经发送的消息。

## 16. 本次不做

- 不把考核实现、题库或审核员放进底座。
- 不引入每人一个 Agent，不开发员工画像新表。
- 不新增 Skill 市场、自动联网安装、脚本执行和复杂插件分发协议。
- 不承诺仅靠 Skill 或提示词彻底阻止模型凭常识猜测答案；需要保留短行为约束、数据投影、现有业务拦截及针对性测试。
- 不声称已经补齐任意工具点续跑；恢复不确定时保持停止副作用的安全策略。
- 不在本文完成后自动修改代码、重启服务或推送 GitHub/GitLab。本文作为已批准方向的开发设计基线。

## 17. 完成定义

任何产品只要登记受控 Skill 包，就能复用底座的发现、读取、完整上下文与依赖执行能力；不使用 Skill 的产品无须适配。产品 A 的考核说明、流程、权限和数据继续留在产品仓库，普通问答不再背负完整考核规则，而真实考核任务具备可核验、可测试的按需加载链路。
