# SQLite 到 PostgreSQL 的显式离线迁移工具

入口：`python -m suiteharness.persistence.migration <operation> --spec /private/spec.json --output /private/new-report.json`。

工具不会导入产品模块，不启动飞书、模型、考核派发、通知或后台任务。SQLite 只读，目标表须事先由各模块显式 schema 迁移创建。当前开发仅在隔离 PostgreSQL 测试库运行，生产迁移和旧库删除尚未执行。

## 操作与输入

所有规范、报告和快照均应保存在仓库外的私有目录；报告文件以 `0600` 新建，禁止覆盖已有报告。DSN 只从环境变量读取，不在规范、命令行参数或报告中存放密码。

- `inventory`：`source` 包括 `path`、`product`、`owner_root`、`writers`。路径必须是归属根下的现有常规文件，路径链不允许符号链接；不自动扫描整个服务器。输出精确路径、归属、生产写入者清单、主文件及 WAL/SHM/journal 指纹、schema、版本、行数、主键和逐表内容摘要。`writers` 是操作者盘点，不是工具已经证明进程停写。
- `snapshot`：同上，另有 `destination`。用 SQLite backup API 生成一致性快照，包含已提交 WAL 内容；不会只复制在线主文件。目标必须是新文件。输出可被后续命令引用的 manifest，绑定快照 SHA256 和逻辑内容摘要。
- `preflight`：`dsn_env`、`target_database`。验证连接、实际库名、PostgreSQL >= 14；报告明确标注空间、角色授权仍需运维独立确认。此项不能替代权限与容量验收。
- `import` / `verify`：`manifest` 文件路径、`dsn_env`、`target_database`、`mappings`。只写显式 allowlist；mapping 必须覆盖快照中每个非 SQLite 内部表，所有源字段必须保留。`mappings` 按外键依赖顺序填写。
- `cleanup-plan`：`source`、`manifest`、`verification` 文件路径及 `evidence`。生成精确路径和指纹清单，不删数据。
- `cleanup`：`plan` 路径、新的 `evidence`，以及与 `verify` 相同的 `manifest`、`mappings`、`dsn_env`、`target_database`。即时重新对账 PostgreSQL，复核归属、主文件及 sidecar 完整集合、逐文件指纹、已验证备份、清理证明与 `/proc/*/fd` 打开文件状态后，删除清单中精确文件，不删除 state 目录、未归属文件或保留备份。

示例（占位路径，不是可执行的生产迁移清单）：

```json
{
  "dsn_env": "SUITE_MIGRATION_DSN",
  "target_database": "feature_assistant",
  "manifest": "/private/sessions.snapshot.json",
  "mappings": [
    {
      "source_table": "sessions",
      "target_schema": "harness",
      "target_table": "sessions",
      "key_columns": ["tenant_id", "product_id", "session_id"],
      "columns": []
    }
  ]
}
```

空 `columns` 表示同名全字段；重命名时必须显式枚举所有 `[源列, 目标列]`。`key_columns` 需要在源数据中非空且唯一；真实 session 主键应依据当前 schema 盘点填写，不能直接照抄示例。`preserve_rowid_as` 可显式指定目标 identity 列，例如考核派题和抄送草稿用 `"preserve_rowid_as": "rowid"`，保留 SQLite 的真实 ROWID，而非按主键排序重建最新顺序；源 WITHOUT ROWID 或所有 rowid 别名均被遮蔽时拒绝该映射。目标列/表缺失、映射遗漏、保护 schema、重复目标或不合法标识符都会阻止导入。业务产品数据映射由产品仓维护；尤其不能把 B 现有 `sales_profile` 业务表当作可覆盖的通用目标。

## 一致性与续跑

迁移 ID 绑定快照 SHA256、产品归属、真实目标库身份和完整表映射（含 rowid 映射）。盘点的逻辑摘要还包括 rowid 与行内容的对应关系。每个目标库持有 session advisory lock，同库另一个导入器明确失败。批次写入和 checkpoint 在同一 PostgreSQL 事务内提交；进程中断后使用同一 manifest 续跑，已提交数据仍要重新核验。

SQLite `table_xinfo` hidden=2/3 的生成列及其建表表达式纳入盘点。导入仅写基础列，不写北京时间等 generated 字段；PostgreSQL 由显式 schema 迁移建立等价 STORED generated 列。`verify` 另外逐值对账源和目标派生值，表达式不同导致的时区、格式、精度差异也会阻止验收。迁移不通过触发器补写历史时间。

遇到同键记录，只在所有字段精确一致时跳过；不一致即终止，绝不通用 upsert 覆盖。字段摘要区分 NULL、空文本、BLOB、整数与浮点，文本按 UTF-8 原文计算；JSON 字符串空白改变也算不同，拒绝 JSON/JSONB 目标列自动重序列化。`verify` 对目标做 repeatable-read 只读事务，检查完整行数、键与值摘要，多余目标行也会失败。数据库约束继续执行，外键失败不会被忽略。

SQLite AUTOINCREMENT 历史序列与目标最大值共同决定下一值，使用事务性的 `ALTER SEQUENCE RESTART`，不使用不能回滚的 `setval`。反复导入不会倒退已使用的 PostgreSQL 序列。目标表必须配置匹配的 identity/sequence；否则报错，不静默丢失编号历史。

PostgreSQL 导入失败时，`MigrationError.diagnostic` 与 CLI 私有失败报告保留 SQLSTATE、异常类型、allowlist 中的源表/目标 schema/目标表、批次 offset。不会保存 PostgreSQL 的 message/detail、SQL、参数、行值或 DSN。例如整数越界可定位为 `22003`，约束失败为 `23514`；同一批次数据与 checkpoint 仍一起回滚。使用这些受控字段定位后，由维护者查看受限日志，不应开启员工原文的普通终端打印。

## 清理条件与边界

`evidence` 所需字段：`writers_stopped`、`consumers_postgresql`、`business_regression_passed`、`postgres_backup_restored`、`sqlite_backup_restored`、`config_references_checked` 都必须为 `true`，`unclassified_paths` 必须为空数组，`evidence_reference` 必须指向实际受控验收报告。

这些字段是操作者对停写、消费者配置、定时任务、DbGate 连接、备份恢复等验收结果的声明，工具不能仅靠文件系统证明产品已切换。必须在外部持续隔离所有旧写入者；`/proc` 扫描同时核对路径和设备/inode，识别容器挂载或硬链接别名仍打开的同一个 SQLite 文件，权限不明时拒绝；它只能发现当前打开文件，不能阻止某个定时任务稍后再次启动。存在未接入 PostgreSQL 的消费者时只交付清理计划，禁止执行 cleanup。

`cleanup` 在实际删除前必须通过真实 PostgreSQL 连接重新执行 `verify`，拒绝只依赖旧验证报告；仍须重新检查外部停写与恢复证明。工具本身不部署产品、不停止服务、不更改定时任务，不自动安排备份过期删除。SQLite 迁移前备份建议保留至少一次恢复验收周期（建议 7 天），到期清理由运维单独确认；cleanup 始终保留 manifest 中的备份。Phoenix、OpenTeamContext、知识库及其他项目不在工具自动盘点范围。

内容摘要当前以排序的逐行 SHA256 汇总，避免数据库排序规则差异；不记录行原文。大规模生产迁移应评估快照备份用时、逐表对账内存与磁盘空间；本工具不据隔离测试结果承诺生产容量或停机时间。
