# 服务器流量看板

通过现有 HTTPS 站点的 `/server-traffic/` 访问，手机与电脑共用响应式页面。先在同一域名登录英语学习管理员账号，再打开看板。普通账号返回 403；未登录返回 401。统计接口每次调用本机 `/api/auth/me` 验证会话，不保存口令、Cookie 或账号数据，认证服务异常时拒绝读取。

## 统计口径

- 服务器总量：默认路由网卡的 RX/TX 字节，不含 loopback。
- Xray、Hysteria 2、TUIC：对应 systemd 服务的 IPIngressBytes/IPEgressBytes，包含客户端与上游连接，也包含本机连接。
- 英语学习：Nginx 进程计数，覆盖静态资源、接口转发，也包含其他 Nginx 请求及本看板访问；不是单站点精确归属。
- Xray 承接英语学习 HTTPS 转发，和 Nginx 存在重复；服务指标不可求和或除以网卡总量得占比。与云厂商计费口径也不同。

首次采样建立基线，此前流量不补算。每分钟读取一次，SQLite 保存累计和小时记录，网页给出今天、近 7/30 天和全部累计，图表最多展示最近 30 天。日期按北京时间；近 7/30 天按整点窗口。服务重启或机器重启后按新计数继续累加；重启前最后一次采样后的尾段不能恢复。采集长时间中断后，尚未重置的增量记在恢复所在小时。异常显示不可用，不伪造为零。速率是相邻采样平均字节/秒。

## 安装

本组件是独立运维脚本，使用服务器已有 Python 3.10+ 标准库；英语学习的 Rust 后端无需改动。需要 systemd IPAccounting/BPF 支持。安装前确认四个服务名与主网卡路由；多网卡机器通过采集服务的 `TRAFFIC_INTERFACE` 环境变量指定所需网卡。

读取仓库外部署配置，通过 SSH 上传本目录到临时目录，在服务器执行 `sudo bash install.sh`。安装会备份 Nginx 片段，启用四个服务的 IPAccounting（不重启它们），安装采集 timer 与只监听 `127.0.0.1:9108` 的网页服务，再校验并 reload Nginx。无需新开公网端口或另发访问密钥。

代码位于 `/opt/server-traffic/`，SQLite 和原子替换的网页快照位于 `/var/lib/server-traffic/`。采集器以 root 运行；网页服务使用 DynamicUser，只能读取快照，不能读取权限 0600 的数据库或写系统目录。数据持续保存，备份时使用 SQLite backup API。每年约 4.4 万条小时记录。

主站发布模板包含 `englishlearning-extra-*.conf` 可选扩展，后续 Rust 发布保留入口。仅回退到不含该 include 的旧主站版本时，需要重新运行安装脚本恢复入口。

## 验证与恢复

本地运行 `python3 -m unittest discover -s deploy/traffic-monitor -p 'test_*.py'` 和 `python3 scripts/check_sensitive_content.py`。服务器检查 timer、网页服务状态、快照更新时间与实际计数增长，并确认公网无会话访问被拒绝。

卸载入口：删除 `/etc/nginx/snippets/englishlearning-extra-traffic.conf`，`nginx -t` 后 reload；停止并禁用 `server-traffic-web.service` 和 `server-traffic-collect.timer`。保留 `/var/lib/server-traffic/` 即保留全部历史。需要关闭计数时，对四个服务运行 `systemctl set-property <unit> IPAccounting=no`。安装输出的备份目录保存原 Nginx 配置。
