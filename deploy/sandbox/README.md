# SuiteHarness Bash 沙箱镜像

这是 `suiteharness.shell.bash` 的最小参考镜像，不是产品案例。构建时必须显式传入带
`sha256` 摘要的基础镜像；部署配置还必须填写最终镜像的摘要，不能使用
`latest` 或浮动标签。

```bash
docker build \
  --build-arg BASE_IMAGE='debian:bookworm-slim@sha256:<真实摘要>' \
  --tag suiteharness-sandbox:0.1.0 \
  deploy/sandbox
docker inspect --format='{{index .RepoDigests 0}}' suiteharness-sandbox:0.1.0
```

运行时框架还会强制只读根文件系统、删除全部 Linux capabilities、启用
`no-new-privileges`、限制 CPU/内存/PID/临时盘，并默认关闭网络。工作区以
UID/GID `65532:65532` 挂载；运维应只对相应租户/产品目录设置这个属主或受控
ACL，不能用 `chmod 777`。Docker daemon 或 socket 本身属于高权限边界，建议
使用 rootless Docker 或独立沙箱执行节点，不要暴露给产品插件。
