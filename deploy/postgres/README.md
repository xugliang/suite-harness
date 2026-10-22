# 产品 PostgreSQL 基础设施模板

此目录由 SuiteHarness 提供通用 PostgreSQL 部署模板。数据库容器独立运行，每个环境只部署一次；产品仅连接自己的逻辑库。底座的存储接口和通用表属于底座，产品库名、产品账号授权和考核/客户等业务表迁移属于产品仓。数据目录、密码文件、备份都在源码仓之外。

模板使用 PostgreSQL 官方 17 镜像，默认仅发布宿主机 `127.0.0.1:5433`。`standalone-postgres` profile 必须显式启用；有现成 PostgreSQL 或托管数据库的部署直接配置 DSN 即可，不需要运行本模板。此模板不创建产品账号或业务表，不给产品运行账号 DDL、superuser 或 Docker 权限。

## 7.22 当前环境

当前产品 PostgreSQL 已存在于 5433，由 rootless Docker 管理，socket 为 `/run/suite-products-db/docker.sock`，容器为 `suite-products-b-postgres17`。本次开发复用已有实例，不执行此目录的启动命令、不接管其数据目录、不增加第三个数据库实例。KnowSeam 的独立 PostgreSQL 保持原有归属。

## 新环境使用

1. 在仓库之外建立私有部署目录、持久化目录、密码文件；限制目录/文件访问权限。`env.example` 仅包含路径示例，不包含真实密码。
2. 设置环境变量后先检查配置：`docker compose --env-file /private/path/postgres.env -f deploy/postgres/compose.yaml config --quiet`。
3. 明确确认本环境没有已有产品数据库实例、端口未占用后，使用 `docker compose --env-file /private/path/postgres.env -f deploy/postgres/compose.yaml --profile standalone-postgres up -d`。
4. 由产品部署维护者使用受控管理员连接创建产品逻辑库、owner、迁移账号、运行账号和只读管理账号。逻辑库名称与产品目录名一致，例如 `feature_assistant`、`sales_profile`。不要把产品名称写死进此模板。
5. 各产品用本产品 DSN 连接。已有数据库部署只执行产品自己的 schema 迁移，不由产品自动启动本容器。

Compose 的 healthcheck 只证明 PostgreSQL 接受连接；应用还需检查自己的 schema 版本和访问权限。持久化目录不跟随容器删除，但仍需独立备份与隔离恢复验证。不得通过 `down -v` 或递归删除数据目录处理应用故障。

模板没有开发发布、产品重启或回滚逻辑。源码运行方式与数据库部署生命周期独立。
