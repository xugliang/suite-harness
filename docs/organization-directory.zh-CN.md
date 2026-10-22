# 单企业组织目录

本模块服务一家企业，提供全产品共享的当前员工、部门及身份映射。它是 SuiteHarness 的可选能力，首版接入飞书通讯录；没有专用应用配置或没有产品需求时不抓取飞书。

## 归属与部署

代码位于 `src/suiteharness/organization/`。`source.py` 的 ABC 定义数据源合同，`feishu.py` 读取飞书，`postgres.py` 保存当前资料和协调状态，`service.py` 管理同步，`http.py/client.py` 为产品提供认证后的接口。

推荐在现有产品 PostgreSQL 实例内建立 `organization_directory` 逻辑库，表位于 `directory` schema；A/B 原有逻辑库、员工业务权限和手工职责维持各自归属。目录代码不依赖产品 A，产品不直接获得专用飞书密钥或目录数据库写权限。

单企业目录不包含 `tenant_id`、企业路由或企业选择。源账号仍保留提供方标识以及飞书应用作用域，这是正确识别员工所需的信息。

`deploy/organization/` 提供源码容器部署、默认关闭配置和无真实凭据的示例。真实配置、数据库口令及调用 token 放到仓库外的权限 0600 文件。目录默认仅监听服务器 `127.0.0.1:18797`，产品可以在同机通过内部接口使用。

## 开启、同步与关闭

1. 管理员显式创建逻辑库、受限角色并迁移目录结构。运行账号仅有目录表所需的 DML 权限，无建表、建库和跨产品业务权限。
2. 配置专用应用、只读通讯录权限及完整数据范围；`app_id` 缺失时模块惰性关闭。配置完整但迁移缺失会明确失败，不建表、不回退 SQLite。
3. 获准产品调用 `OrganizationDirectoryClient.acquire()`，以服务 token 注册需求。token 对应的产品由服务端确定，不接受请求体声明的产品身份。
4. 客户端每 120 秒续登记，默认 600 秒到期。至少一个消费者有效时保持每日同步；无人使用时同步休眠，资料不自动删除。
5. 成功核验不足 24 小时直接复用。数据库租约和 fencing token 保证进程之间去重；提交时再次核验。失去租约只放弃当前抓取，不永久停止后台循环。
6. 完整分页后比较内容摘要，差量写入；目录和成功时间在同一事务提交。抓取期间不持有数据库长事务。无变化也刷新核验时间。

HTTP 429、服务端错误及瞬时网络故障最多调用三次（首次加两次重试），随后默认退避 300 秒。权限和数据完整性错误不盲目重试。飞书鉴权失败、确认撤权、字段权限缺失或范围收窄会限制旧目录可见性；网络故障保留当前资料并报告更新时间。

首版是“完整读取核对、增量写库”，不是飞书变更流。用户组范围尚不支持展开，遇到非空 `group_ids` 明确拒绝不完整同步；可通过飞书全企业部门范围使用。每页必须含合法 `has_more`，游标重复、漏页或部分请求失败不会发布。

## 当前版与身份

表包括 `employees`、`departments`、`employee_departments`、`external_identities`、`principal_bindings`、`sync_state`、`consumers` 及结构版本元数据。没有员工历史快照表。

同步缺失人员表示“当前不再可见”，不直接宣布离职。清除其姓名、岗位与部门等资料，只留下不可用状态、内部员工 ID 和必要身份关联，以保护历史业务引用。异常全空快照拒绝覆盖已有目录。

`employee_id` 是内部身份，不用姓名自动合并。飞书 `open_id` 带应用作用域；`union_id` 只能在同一应用服务商范围使用；`user_id` 仍可能修改或复用。新旧稳定身份一致时可沿用内部 ID；同一编号对应稳定身份不同则拒绝关联，防止把新员工混入旧人的人工职责和账号。

已有产品账号需要管理员先核验外部身份，然后使用本地管理 CLI `bind` 建立 `(product_id, principal_id)` 到员工 ID 的关联。该操作没有 HTTP 写接口或 LLM 工具。未来新增产品可通过自己的可信身份流程接入，不能按姓名自动创建绑定。

## 产品调用

```python
from suiteharness.organization.client import OrganizationDirectoryClient

client = OrganizationDirectoryClient(
    "http://127.0.0.1:18797", private_token,
    product_id="your-product",
)
await client.acquire()
employee = await client.get_employee_for_principal(authenticated_principal_id)
departments = await client.list_departments()
await client.close()
```

`clients` 配置可分别控制是否允许目录搜索、员工返回字段以及部门返回字段。部门负责人源账号不直接暴露。业务权限仍由产品判断，组织身份本身不授予管理员、考核员或客户访问权限。

产品 A 可在原 `work_profiles` 配置下增加 `directory_url` 和 `directory_credentials_ref`，私有 `services` 配置提供 token。新目录启用后，产品 A 不再启动原飞书组织同步，提示词只临时合并共享组织事实和原有人工职责，不建立本地组织镜像。新员工维护支持 `run_employee_profiles.py ... show --principal ...`；人工导入同样先核对明确绑定。

查询失败、未绑定、状态未知/离职或超过产品可接受的新鲜度时，个性化降为通用提示词，不回退旧组织缓存。目录不替代产品登录或实时离职权限回收机制。

## 管理命令与升级

```bash
python -m suiteharness.organization --config /private/organization.yaml --secrets /private/organization.secrets.yaml status
python -m suiteharness.organization --config /private/organization.yaml --secrets /private/migration.secrets.yaml migrate
python -m suiteharness.organization --config /private/organization.yaml --secrets /private/organization.secrets.yaml bind --product your-product --principal verified-principal --identity-kind user_id --identity-value verified-user-id
```

迁移命令需要管理员凭据；正式运行文件只保存运行角色凭据。`status` 管理命令显示同步元数据，不包含员工资料或密钥。

切换数据源应用属于管理员操作。先检查旧配置 fingerprint、停止旧实例，再使用新配置执行 `activate-config --expected-config-fingerprint <旧值>`；CAS 操作使旧配置实例不能重新夺取同步权。新数据完整发布之前，HTTP 状态为 pending，拒绝将旧应用资料当作新配置结果返回。普通产品调用不能切换配置。

首次发布不允许把目录查询接到无身份校验的公开接口；框架不将这些接口自动注册成 Agent 工具。若产品需要提供通讯录问答，产品自己决定可用字段和调用者权限。

## 验证

离线测试替换外部 HTTP，不调用模型或给真实员工发送消息。PostgreSQL 测试必须显式提供 `SUITE_ORGANIZATION_TEST_DSN`，且目标库名固定为 `organization_directory_test`；没有测试凭据就跳过真实库用例，不推断生产连接。

范围覆盖：完整分页、字段权限、未知状态、并发去重、失败回滚、权限收窄、配置切换、租约丢失、编号改名和复用、产品 token 与字段投影、客户端恢复续登记及产品 A 人工职责合并。
