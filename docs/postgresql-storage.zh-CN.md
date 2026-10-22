# SuiteHarness PostgreSQL 存储说明

本文说明底座 PostgreSQL 适配器、配置、显式迁移和验证入口。它是开发与接入说明，不代表 7.22 的生产 SQLite 已经迁移或删除。本次开发不执行生产切换、产品重启和 SQLite 删除。

## 数据归属

产品数据库实例供多个产品使用，每个产品拥有独立逻辑数据库。逻辑库名称与产品目录一致，例如 `feature_assistant`、`sales_profile`。各产品使用自己的连接账号和 DSN。SuiteHarness 的会话、事件、运行快照、工具结果和通用产品状态存放在该产品库的 `harness` schema；考核、画像、客户资料等业务表由产品仓维护。

KnowSeam 继续使用独立的知识库 PostgreSQL 实例。Phoenix、OpenTeamContext 和第三方工具的数据库不属于本次底座存储迁移范围。

## 安装依赖

在底座源码目录安装 PostgreSQL 的第三方可选依赖；底座源码继续通过 PYTHONPATH 指向 src 使用：

```bash
python -m pip install 'psycopg[binary,pool]>=3.2,<4'
export PYTHONPATH="$PWD/src"
```

`postgres` extra 对应 `psycopg[binary,pool]>=3.2,<4`。运行 PostgreSQL 后端需要该依赖；仅使用兼容 SQLite 后端不需要启动 PostgreSQL 或安装这个 extra。

## 配置与凭据

以下是合并到完整产品配置中的 `storage` 片段。示例路径和引用名不是真实部署凭据：

```json
{
  "storage": {
    "root": "/var/lib/suite-products/feature_assistant/state",
    "backend": "postgresql",
    "postgres": {
      "dsn_secret_ref": "product_database",
      "schema": "harness",
      "pool_min_size": 1,
      "pool_max_size": 6,
      "connect_timeout_seconds": 10,
      "pool_timeout_seconds": 10,
      "statement_timeout_ms": 30000,
      "lock_timeout_ms": 5000,
      "idle_transaction_timeout_ms": 15000
    }
  }
}
```

`root` 仍需提供绝对路径，但 PostgreSQL 会话、审计、工具结果和状态不写入该路径下的 SQLite 文件。PostgreSQL 模式不能同时指定旧的 `sessions_database`、`audit_database`、`runtime_database` 文件字段；相应的 `*_path()` 接口也拒绝返回 SQLite 路径。

实际 DSN 只放到独立、受限访问且不进 Git 的 secrets 文件中：

```json
{
  "databases": {
    "product_database": {
      "dsn": "postgresql://RUNTIME_USER:REPLACE_WITH_PRIVATE_PASSWORD@DB_HOST:5433/feature_assistant"
    }
  }
}
```

`dsn_secret_ref` 对应 `secrets.databases` 的键。缺少对应凭据直接报错，不改用 SQLite。迁移操作和产品运行可以使用不同 secrets 文件，引用名保持一致：迁移文件提供可建表的迁移账号，产品运行文件提供本产品低权限账号。运行账号需要读写底座表和使用审计 identity sequence 的权限，不应拥有 DDL、跨产品访问或 Docker 管理权限。

配置中的 pool 大小以单个产品进程计。一个进程内各底座 store 共用一个受控连接池；部署多个进程时，应按总进程数核算 PostgreSQL 连接额度。配置与模型表示中的 DSN 使用 `SecretStr` 脱敏，日志和运维报告中也不应打印原始 DSN。

## 原目录中的装配入口

代码位于原仓库 `src/suiteharness/server/storage.py`：

1. `create_runtime_database(loaded)` 根据明确选择的后端构造数据库对象。PostgreSQL 构造阶段不连接数据库。
2. `create_persistence_runtime(loaded)` 使用同一个 `PostgresDatabase` 构造 session、audit、tool-results、product-state、MCP resume、sandbox quarantine 等 store。
3. `open_runtime_database(database)` 在生命周期启动阶段调用异步 `open()`，建立池并只读核验显式迁移后的 schema 版本。
4. 渠道事件去重也使用该进程的共享 PostgreSQL 数据库与明确的事件作用域。

主要实现文件：

| 文件 | 职责 |
| --- | --- |
| `persistence/postgres.py` | 池生命周期、事务、有限 SQL 方言、产品状态、渠道去重、MCP、sandbox quarantine |
| `persistence/postgres_schema.py` | 显式 PostgreSQL DDL、schema 版本目录、北京时间派生列和通用函数 |
| `sessions/postgres.py` | 会话、revision、transcript、checkpoint、run lease、clear receipt |
| `execution/postgres.py` | 审计 journal、执行所有者隔离的工具结果与清理 tombstone |
| `persistence/manage.py` | 独立迁移、检查和版本查询命令，不激活产品服务 |

## 显式管理命令

以下命令在底座源码根目录执行；`--config` 指向完整配置，`--secrets` 指向受限访问的凭据文件。参数通过文件提供，不把密码放进命令行。

```bash
PYTHONPATH=src python -m suiteharness.persistence.manage migrate --config /private/product.config.json --secrets /private/product.migration.secrets.json
PYTHONPATH=src python -m suiteharness.persistence.manage check --config /private/product.config.json --secrets /private/product.runtime.secrets.json
PYTHONPATH=src python -m suiteharness.persistence.manage versions --config /private/product.config.json --secrets /private/product.runtime.secrets.json
```

`migrate` 只安装受控的底座 PostgreSQL schema 和版本记录，可重复执行；不负责搬迁已有 SQLite 行，也不创建产品考核、客户等业务表。产品业务迁移、历史导入、行级对账和备份恢复验证由对应的产品迁移入口负责。

`check`、`versions` 只连接并检查，不执行 DDL。运行时启动同样不会自动建表、自动升级或者退回 SQLite。缺表、未迁移、版本不兼容或连接失败会使接入失败，需由维护者处理显式迁移。输出包含实际逻辑库、schema、组件版本和是否启动产品服务，不输出凭据。

底层 `PostgresDatabase` 还支持可选的 `expected_database`：传入受信任的预期逻辑库名后，`open()` 和 `migrate()` 会核实 `current_database()`，错误连接目标在执行迁移 DDL 前被拒绝。产品装配还应按本产品目录名称校验目标库。

## 共享业务状态机与 PostgreSQL 方言边界

实现复用原有 SQL 业务状态机，提炼为 `SQLSessionOperations`、`SQLAuditOperations`、`SQLToolResultOperations` 及相关 `SQL*Operations`。SQLite 子类保留自己的文件连接和初始化逻辑，PostgreSQL adapter 使用独立的 PostgreSQL operation 对象与受控连接池。PostgreSQL 路径不会调用 SQLite 构造器，也不会创建 SQLite 连接。

这里不是任意 SQLite SQL 的自动迁移器。有限方言层只处理已审核的底座 SQL：qmark 参数绑定、行字段访问、已有 `INSERT OR IGNORE` 的冲突忽略写法，以及 checkpoint UTF-8 字节配额的 PostgreSQL 表达式。SQL 数据始终通过参数绑定。PostgreSQL DDL 在 `postgres_schema.py` 中显式定义，不从 SQLite 建表语句推测类型或自动替换 schema。

JSON 和 ISO 时间继续使用原有文本表示，以保留历史内容、摘要及幂等摘要的原值。revision、sequence、计数和微秒 epoch 使用 `BIGINT`，审计全局 sequence 使用 PostgreSQL identity。业务层继续保留 revision CAS、幂等冲突检测、clear receipt、已清理内容的 digest tombstone、run/event owner 和 lease 的规则。

阻塞的 psycopg 和 pool 操作都在线程中执行，不阻塞产品的异步事件循环。每次 store 操作对应一个完整数据库事务。取消调用和关闭池时会等待已开始的原子操作结束，避免把仍可能提交的写操作遗留在后台。

为了维持现有全局配额、查后写和 clear/lease 的一致性，同一个逻辑库、同一个 schema 的写事务使用 PostgreSQL advisory lock 串行化；读操作使用 repeatable-read 快照。不同产品逻辑库独立。这是明确的吞吐权衡：连接池可以并发读，但同一 schema 写吞吐仍受全局写锁限制。后续若改为细粒度行锁或更高并发，需要同时重做全局配额和查后写的并发验证，不能只删除锁。

## 北京时间派生列

现有 SQLite 的 `*_beijing` 是 `GENERATED VIRTUAL` 派生列。PostgreSQL 保留同名 `GENERATED ALWAYS ... STORED` 列，根据原始 ISO 时间或 epoch 维护固定 UTC+8 的可视化值，不改变原始 UTC 字段。

通用函数 `suiteharness_beijing_iso(text)`、`suiteharness_beijing_epoch(double precision)` 提供确定的秒级显示、非法日期返回 NULL，以及与 SQLite 毫秒舍入一致的边界行为。产品迁移可复用 `beijing_function_ddl(schema)`；每个返回项是一条完整函数 DDL，不能再按函数体内的分号拆分。

历史导入必须识别 SQLite `PRAGMA table_xinfo` 的派生列并跳过写入这些列，导入原始时间后单独核对派生显示。不能尝试直接写 PostgreSQL generated 列。

## 可选基础设施模板

`deploy/postgres/` 提供 PostgreSQL 官方镜像的可选 Compose 模板、非机密环境示例和说明。它是每环境一次的独立数据库服务，不是每产品启动一个容器，也不要求底座再运行一个服务容器。

有现成 PostgreSQL 或托管数据库的部署直接配置 DSN。7.22 已有产品 PostgreSQL，不能为了使用本模板在已占用端口再启动实例。产品逻辑库、账号和业务表的清单属于产品仓；数据目录、真实密码和备份位于仓库之外。详细参数见 `deploy/postgres/README.md`。

## 验证方式与当前状态

真实 PostgreSQL 测试只使用维护者准备的独立测试逻辑库，不使用生产库。把测试 DSN 从受限文件注入 `SUITE_POSTGRES_TEST_DSN` 后执行：

```bash
PYTHONPATH=src:tests python -m pytest tests/test_postgres_foundation.py
PYTHONPATH=src:tests python -m pytest
```

`test_postgres_foundation.py` 也兼容 `SUITEHARNESS_TEST_POSTGRES_DSN`。未设置测试 DSN 时，真实数据库用例明确 skip；不能把 skip 解释为真实 PostgreSQL 验证完成。测试为每例创建随机专属 schema，结束后仅清理本例 schema，不发送飞书消息。

验证覆盖连接与 schema 约束、错误库名阻断、运行时零 DDL、无 SQLite 回退、会话 revision 并发、checkpoint/clear 幂等、run/event lease 与 owner、工具结果分页/身份隔离/配额/迟到写入拒绝、MCP 状态隔离、quarantine 所有权、审计重复事件、事务回滚、事件循环非阻塞、关闭时保护在途写入以及北京时间派生值。原 SQLite 兼容测试继续保留，用于验证共享状态机提炼没有破坏既有行为。

代码、测试和试迁移通过并不等于完成生产迁移。删除原 SQLite 前，还需对所有真实消费者及后台任务完成切换核实、历史数据对账、数据库授权验证、独立备份恢复验证，并确认不会再生成旧 SQLite。当前是否已经切换，以独立迁移报告和真实配置核验为准。
