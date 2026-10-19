# VPS 升级与 macOS 隧道

## 原生 VPS 升级

原生 systemd 部署可从仓库根目录执行升级入口。每次显式给出 SSH 主机；默认查询 GitHub 最新正式版、
显示版本、迁移和备份预览，并在更改前询问一次：

    just vps-upgrade host=<vps-host>

只检查和预览：

    just vps-upgrade check host=<vps-host>

yes=true 跳过交互确认。升级要求 SSH root 访问 Linux amd64 VPS，以及 systemd、nftables、PostgreSQL 客户端
和 Python 3。脚本通过 Release 的 checksums.txt 校验归档，并将发布标签解析为固定提交来核对 SQLx 迁移，
无需手动提供迁移编号。

若当前二进制能提供可解析的正式版本号，脚本会拒绝降级。旧版二进制不支持 `--version` 时，预览显示
二进制摘要；只有数据库迁移与目标 Release 完全匹配且严格落后于目标迁移时才允许升级，迁移较新或 checksum
不符仍会停止。

确认后，VPS worker 会先下载并校验发布归档。归档包含 `plugins/official` 时，还会校验官方插件清单的
校验和与发行身份。此时服务继续工作；
只有归档有效且服务仍符合预览结果时，才启用临时 nftables 规则，停止服务并备份当前二进制、静态资源、
二进制旁的 `plugins/official` 发行目录、配置和 PostgreSQL 数据库。
健康检查通过一个带 socket 标记的本机请求完成。数据库迁移、静态资源、官方插件目录和运行中二进制校验通过后才恢复
访问。升级中的短暂中断会断开进行中的请求。

当前发布归档不附带官方插件。升级入口兼容包含 `plugins/official` 的归档，该目录由目标发行版完整替换，
仅用于随发行版提供的官方插件，不用于保存独立安装的插件或运行数据。目标归档不含该目录时，升级会移除
当前目录；原目录保留在上一份部署备份中，升级失败时一并还原。

上一份可恢复部署保留在 CPR_VPS_BACKUP_ROOT/previous。新服务无法启动或检查失败时，worker 会在请求
仍被阻断期间还原数据库与应用文件，再验证原服务。恢复失败会保持阻断并保留备份。使用命令输出的
Remote status 路径检查 SSH 断线后的结果；worker 由 VPS systemd 独立运行，不受本地终端断开影响。
成功升级后才会轮换上一份备份和清理旧脚本生成的升级备份。

## macOS 本地 SSH 隧道

需要从 macOS 访问仅绑定在 VPS `127.0.0.1:8080` 的实例时，使用
`deploy/macos/vps-tunnel-launchd.sh` 安装用户级 LaunchAgent。脚本只监听本机回环地址，
不会把代理端口暴露给局域网；生成的 plist 不提交到仓库。

从可信的固定 tag 或 commit 获取与部署版本匹配的脚本。当前私有 fork 的临时工作分支可按需替换
下面的 `<ref>`，不要依赖可变的 `main`：

```bash
git clone --branch <ref> git@github.com:ssh352/codex-proxy-rs.git
cd codex-proxy-rs
```

SSH 私钥和 `known_hosts` 必须先由当前 macOS 用户配置好；LaunchAgent 使用持久化的私钥文件，
不会继承当前终端的 SSH agent。安装使用 `BatchMode=yes`、`IdentityAgent=none` 和
`StrictHostKeyChecking=yes`，不会提示输入密码，也不会自动接受新的主机密钥。

安装前提供 VPS、账号和两端端口：

```bash
export CPR_VPS_HOST='<vps-host>'
export CPR_VPS_USER='<ssh-user>'
export CPR_TUNNEL_LOCAL_PORT='18080'
export CPR_TUNNEL_REMOTE_HOST='127.0.0.1'
export CPR_TUNNEL_REMOTE_PORT='8080'

deploy/macos/vps-tunnel-launchd.sh install
```

安装会检查 SSH 认证、端口占用和 `/healthz` 响应，并在以下位置创建服务与日志：

- LaunchAgent：`~/Library/LaunchAgents/com.codex-proxy.vps-tunnel.plist`
- 日志：`~/.codex-proxy/logs/vps-tunnel.{out,err}.log`

全新安装默认加载到 `user/<uid>` 域，因此不要求 `zhang` 保持 GUI 登录；如果该服务已经加载，
后续管理命令会继续使用实际域，不会因为 GUI 会话出现或消失而自动迁移。需要迁移时显式指定目标域，
迁移会短暂中断隧道并先卸载旧域服务：

```bash
deploy/macos/vps-tunnel-launchd.sh install --domain gui
# 或：CPR_TUNNEL_DOMAIN=user deploy/macos/vps-tunnel-launchd.sh install
```

`gui/<uid>` 适合依赖 GUI 会话的服务，退出 GUI 后隧道可能停止；`user/<uid>` 适合长期后台运行。
如果同一 label 同时加载在两个域，脚本会报错，需先手动卸载其中一个。

维护命令从同一固定版本的脚本执行：

```bash
deploy/macos/vps-tunnel-launchd.sh status
deploy/macos/vps-tunnel-launchd.sh health
just vps-tunnel-app-launchd-status
deploy/macos/vps-tunnel-launchd.sh restart
deploy/macos/vps-tunnel-launchd.sh stop
deploy/macos/vps-tunnel-launchd.sh start
deploy/macos/vps-tunnel-launchd.sh uninstall
```

`uninstall` 只卸载该 LaunchAgent 并删除其 plist，保留日志。安装或重启失败时不要停掉其他服务；
先查看 `status` 和 stderr 日志，再处理 SSH 密钥、主机校验或远端健康检查问题。

`just vps-tunnel-app-launchd-status` 在显示 LaunchAgent 状态后还会请求本机转发的 `/healthz`；
健康检查使用已安装 plist 中保存的本地转发端口，不依赖当前终端的
`CPR_TUNNEL_LOCAL_PORT`。命令只有在该端点返回成功的 HTTP 状态时才退出成功。
