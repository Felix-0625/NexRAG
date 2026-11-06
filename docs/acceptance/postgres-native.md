# 原生 PostgreSQL 专用只读角色验收

2026-10-29 在独立的本机人工合成集群实测，不使用容器、不安装 Windows 服务，也没有访问已有业务库。结果见 机器记录。这属于服务集成证据，不能替代管理员网页配置连接、员工对话或真实企业数据库验收。

## 来源与隔离

[PostgreSQL 官方 Windows 页面](https://www.postgresql.org/download/windows/) 指向 EDB 的便携 ZIP。[发布方维护者确认](https://github.com/EnterpriseDB/edb-installers/issues/746) 的 `postgresql-18.6-5-windows-x64-binaries.zip` SHA256 为 `e2246ba91d22345bc3d017586c09ede52d9df180b1eeb480f050445f1cad84e2`，与实际下载的 384620317 字节文件一致。核对解压归档 20554 个文件的存在性与大小后运行。

测试服务为 PostgreSQL 18.6，`listen_addresses=127.0.0.1`，使用 SCRAM 密码认证。数据库、角色名和密码均随机生成；集群与临时凭据保留在被 Git 忽略的 `.local-run/postgres-acceptance/`，供后续网页验收。公开报告不包含连接口令。它不是默认产品依赖，也不承担生产数据。

## 已实测的合同

- `ensure_postgres_reader` 接受专用 reader；拒绝超级用户、拥有表写权限的 writer 和带 `CREATEROLE` 的 manager。
- reader 直接写源表被 PostgreSQL 拒绝；Agent 的 UPDATE/DELETE 在 SQL 安全层拒绝。源表不暴露为可查询的沙箱视图。
- 通过实际 `PostgreSQLAdapter` 查询：A 仅有 1000、250 两行，总额1250；B 仅有9900一行。A 使用 B 的记录条件不能扩大访问范围。
- 撤销 reader 的源表 SELECT 权限后，下次请求拒绝；恢复后正常。
- 人工合成源库所有者明确更新后，新请求看到1750，恢复后回到1250。该证据证明逐请求快照刷新，不宣称 CDC、事件推送或正在运行的旧查询即时变更。
- 测试拒绝操作之后，源数据逻辑 SHA256 保持 `fb011b6b72d5bdfc99fe1ed5bf612dd5ca48fe8430e772faefb9b47dada3add7`。所有者的演示更新亦恢复。
- 管理员连接发现功能接受 reader，拒绝 writer。停止并重启此独立测试集群后，reader 可重连并读取保留的合成记录。

服务账号读取源表，与员工访问应用沙箱是两层权限。reader 凭据只属于后端连接配置，不分发给员工；租户行过滤由已认证的应用身份选择，不能把“数据库账号只读”误称为完整租户隔离。

## 复现

先从发布方下载并校验便携 ZIP，解压到忽略目录中的 `tools/pgsql`。在有已安装项目依赖的 Windows PowerShell 运行：

```powershell
.venv/Scripts/python.exe scripts/start_postgres_fixture.py --tools .local-run/postgres-acceptance/tools/pgsql
.venv/Scripts/python.exe scripts/verify_postgres_native.py --admin-dsn-file <上步返回的admin_dsn_file> --output-dir .local-run/postgres-acceptance/new-verification --keep-fixture
.venv/Scripts/python.exe scripts/start_postgres_fixture.py --existing <上步集群directory> --stop
```

`--output-dir` 必须是新目录，防止覆盖旧验收数据。不传 `--keep-fixture` 时验证器清理自身新建的库和角色；传入时为网页接入保留。启停帮助程序只接受本工作区隔离目录的集群描述，不做递归删除。

受限 Windows 执行令牌第一次使 `pg_ctl` 报 restricted-token 错误；通过明确的本机测试执行授权后，集群运行成功。随后修复 pg_ctl 子进程继承捕获管道导致帮助程序等待的问题，改为日志文件与有界超时；同一集群的停止、启动命令实际正常返回。没有把这些环境或辅助脚本失败算作连接器通过。
