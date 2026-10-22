# 通用源码交付实施计划

**Goal:** 提供公司服务器源码容器、只读诊断、CI 和可审查的干净源码候选目录。

**Architecture:** `python -m suiteharness.doctor` 只读取源码、依赖和显式配置；`tools/build_release.py` 在检查允许列表、敏感内容和路径后创建全新目录。容器通过受信的 ASGI factory 接入产品；独立 smoke app 仅验证进程/HTTP，不宣告业务就绪。

**Tech Stack:** Python 3.11+、现有配置加载器、pytest、Docker Compose、GitHub Actions。

**Spec:** 已批准基础设计第6节。无 SSH、部署、依赖安装、GitHub 推送；不改核心执行/模型/记忆/MCP，不引入业务产品或第三方源码。

## 1. 只读诊断与安全发布

- [ ] 新建 `tests/test_source_delivery.py`：子进程调用 doctor，验证缺失配置失败且不创建文件；发布构建拒绝凭据、私有地址、符号链接、已有目标；只包含允许的源码。
- [ ] 运行 `python -m pytest tests/test_source_delivery.py`，确认缺失工具导致失败。
- [ ] 新建 `src/suiteharness/doctor.py`、`tools/build_release.py`；诊断用现有 ConfigLoader，任何错误只输出固定安全原因，不回显凭据。
- [ ] 重新运行测试至通过。

## 2. 源码容器与中文交付文档

- [ ] 新增 `deploy/source/Dockerfile`、`compose.yaml`、`entrypoint.py`、`smoke_app.py` 和通用配置/空密钥样例；容器非root、只读根、独立数据卷、CPU/内存/PID上限，不挂载 Docker daemon socket。
- [ ] 新增精确依赖版本清单和维护说明；保留现有 MIT LICENSE 并添加设计来源声明 NOTICE。
- [ ] 更新 README、新增 `docs/source-delivery.zh-CN.md`，说明 rootless 沙箱/代理前提、公司 factory、备份/回退及诊断不是 readiness。
- [ ] 添加 CI：Linux Python 基础测试、安全发布扫描、源码镜像构建/HTTP smoke；受限 rootless 沙箱真实探针仅手动在公司自托管 runner 执行。
- [ ] 运行新增行为测试，构建全新候选目录，记录文件清单和哈希；容器与实际沙箱验证留给具备授权环境的主任务。
