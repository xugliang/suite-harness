# 中断与恢复审查整改记录

日期：2026-09-16。基于 `385458a` 的代码审查结果整改；按用户要求，不修改第 3 项共享飞书停止指令的归属机制。

## 修复范围

| 原审查项 | 修复内容 | 关键回归测试 |
| --- | --- | --- |
| 1：旧运行恢复覆盖新状态 | 从新快照合并目标终态，以同一快照版本号提交；冲突重新读取。保护较新的活动运行、不确定运行恢复及连续崩溃恢复。Markdown 归档采用跨进程原子更新和版本比较，防止回退及重复追加。 | `test_run_control_recovery_regressions.py`、`test_uncertain_recovery_race.py`、`test_archive_recovery_atomic.py` |
| 2：恢复绕过当前工具策略 | 保存允许复用的原始参数，恢复时重新检查工具、参数规则、授权策略和审批要求；拒绝、异常或需要审批时不复用旧结果。 | `test_resume_authorization_policy.py` |
| 4：模型默认地址变化未使恢复点失效 | 恢复指纹包含主线路、后备线路、服务商定义、实际端点和适配器；不解析或保存密钥。 | `test_resume_model_fingerprint.py` |
| 5：续跑消息 ID 未绑定操作和源任务 | 幂等摘要绑定续跑操作及来源运行；相同 ID 不能切换目标或改作普通消息。 | `test_run_control_recovery_regressions.py` |
| 6：恢复点缺少物理回收和独立预算 | 增加单点、会话和全局容量预算；过期及成功/失败终态回收，保留原始消息与幂等证据。 | `test_session_resume_retention_and_pages.py` |
| 7：历史响应过大、无法读取旧页 | 增加排他序列游标、逐页授权及实际 JSON 字节预算；过大单条明确截断，旧适配器明确报告不支持分页。 | `test_history_pagination.py` |

## 兼容性

- 升级前缺少原始工具参数或使用旧模型指纹的恢复点可能无法继续执行；重新提问即可。既有聊天记录不会因此删除。
- 恢复点是临时缓存：默认有效期 300 秒；容量限制及分页接口见模块说明和安全模型。
- 本次不调整共享飞书会话的停止权限归属，原审查第 3 项仍是已知未处理事项。

## 验证与交付边界

- Windows 本地回归：647 项通过，13 项跳过；包括真实双进程归档锁及双 SQLite 实例并发恢复。
- 全套运行排除了 `tests/test_mcp_broker_sessions.py` 的 4 项测试，因为当前环境缺少 Starlette；13 个跳过项涉及 Windows 符号链接权限、Unix 专有功能以及缺少 Starlette 的沙箱接口测试。
- Linux `fcntl` 锁分支未在本机实际运行；不能将本次结果称为服务器部署验收。
- 本次仅修改源码、测试及文档；未连接或重启业务服务器。源码发布不代表部署环境已更新。

验证命令（使用任务独立临时目录）：

```powershell
python -m pytest -o pythonpath=src -o addopts='--import-mode=importlib' -q --ignore=tests/test_mcp_broker_sessions.py --basetemp=<任务临时目录> -p no:cacheprovider --tb=short -ra
```
