# Rust 发布与恢复

生产运行目录只包含 `bin/english-server`、`bin/englishctl`、`static/`、部署文件和公共内容种子。不包含 Python、venv、Node、Cargo 或待启动时解析的大型 JSON。原 Python 源码与内容制作脚本留在开发仓库中，用于离线制作、兼容性对照和历史回退。

## 构建与发布前验证

在开发机固定 Rust 1.99.1 和 Cargo.lock，生成 x86_64、glibc 2.35 release；SQLite 与 OpenSSL 静态链接进产物。不要在 1 GiB 生产服务器编译。运行格式检查、Clippy、Rust 单元测试和 Python/Rust 接口及数据库对照。前端使用 `npm run build` 生成静态文件。

`deploy/build-rust-release.sh` 使用 `ENGLISH_BINARY_DIR` 中的二进制和新目录 `RELEASE_OUTPUT` 打包。可用 `ENGLISH_PUBLIC_SEED` 加入经过脱敏检查的 `public-seed.db.gz`。`SHA256SUMS` 覆盖运行文件，`SOURCE_COMMIT` 记录构建源码提交。既有数据和密钥始终留在服务器，不进入 Git 或发布包。

服务器内部运行 `rust_preflight.py compare` 和 `rust_http_preflight.py compare`，通过 SQLite backup API 创建两份隔离快照，对照目录字节、真实数据接口和学习动作，测量相同主机上的延迟、吞吐、CPU 与峰值 RSS。真实数据库仅以只读方式作为快照源，不下载到本机。Python 比较工具仅在验证阶段使用。

## 首次独立运行

安装 Nginx、系统 CA 证书和与产物匹配的 glibc；无需安装 Python、Node 或 Rust。新建受限的数据目录后，解压公共种子并通过 CLI 安装备份：

```sh
gzip -dc /release/content/public-seed.db.gz > /tmp/public-seed.db
/release/bin/englishctl inspect --database /tmp/public-seed.db
/release/bin/englishctl backup --database /tmp/public-seed.db --output /data/vocabulary.db
/release/bin/englishctl create-admin --database /data/vocabulary.db --password-stdin
```

`englishctl init --database /data/vocabulary.db` 也可创建空库，之后用原生内容管线填充。建库与备份拒绝覆盖已有文件。公共种子包含完整公共内容及词形/课程映射，账号、会话、通行密钥、订阅和学习状态为空。

配置沿用 `COOKIE_SECURE`、`REGISTRATION_CODE`、`DEFAULT_TIMEZONE`、`WEBAUTHN_RP_ID`、`WEBAUTHN_ORIGINS`、`WEB_PUSH_PRIVATE_KEY`、`WEB_PUSH_SUBJECT` 和 `WEB_PUSH_ORIGINS`。VAPID 私钥和 WebAuthn RP ID 必须保留，才能继续使用旧订阅与旧通行密钥。

## 切换

将发布目录安装到 `/opt/englishlearning/releases/<release>`，root 所有、目录可由服务及 Nginx 读取。先在隔离快照验证二进制、动态依赖、完整性与原生提醒 dry-run。

以 root 运行：

```sh
/release/deploy/switch-rust-release.sh /opt/englishlearning/releases/<release>
```

脚本备份旧 release 指向、systemd、Nginx 和一致性数据库到新的 `/var/backups/englishlearning-rust-<UTC>/`。暂停提醒，停止旧 API，备份数据库，原子切换 release，安装原生 API/提醒单元并移除旧 Python ExecStart 覆盖。Nginx 保留域名、证书、IPv4/IPv6 监听，使用发布目录的静态前端，将 `/api/` 转发给 Rust，并拒绝公开 `/internal/`。通过 readiness 和 HTTPS 健康/版本检查后才恢复定时器；失败自动恢复原服务配置。

`englishlearning.service` 限制 256 MiB，提醒任务限制 96 MiB。API 采用固定 1 写/2 读连接与 16 个请求槽；普通请求 64 KiB，Passkey 128 KiB，审核交换 2 MiB。读查询 5 秒、写请求 10 秒。目录冷构建串行、30 秒；传输最多四路，每路 64 KiB 缓冲，单文件最大 256 MiB，磁盘缓存保留上限 512 MiB。资源耗尽返回 503 和 Retry-After，取消任务会停止排队查询。

## 回退与排查

```sh
/release/deploy/switch-rust-release.sh --rollback /var/backups/englishlearning-rust-<UTC>
```

回退恢复旧 release、Nginx 和 systemd，并继续使用当前数据库，保留切换后的学习记录。不要直接用旧备份覆盖已有新答题记录。若确需数据恢复，应先停止写入、保存当前快照并核对差异。

排查使用 `systemctl status`、`journalctl -u englishlearning.service`、`journalctl -u learning-reminders.service`、`englishctl inspect`、`free -m` 和内核 OOM 记录。确认 `/proc/<MainPID>/exe` 指向发布的 Rust 产物，NRestarts 稳定，旧 uvicorn/Python 提醒任务未在生产路径运行。

浏览器验收覆盖登录、账户、词典、学习、设置、管理以及页面刷新；通行密钥签名、旧 COSE 凭据、挑战消费和重放拒绝由协议测试覆盖。设备特有问题按后续实际反馈处理。
