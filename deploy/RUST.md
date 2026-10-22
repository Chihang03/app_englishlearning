# Rust 目录构建器部署

第一批只替换词义目录冷构建。Python 继续处理认证、学习、推送和 HTTP 文件发送；Rust 服务骨架尚不接收公开流量。

## 发布产物与验证

在开发机或构建环境生成 Linux release 二进制，固定 Cargo.lock 和 Rust 工具链。当前服务器是 x86_64、glibc 2.35；发布前重新确认。发布目录建议 `/opt/englishlearning/native/<commit>/englishctl`，root 所有、0755，不在服务器执行 cargo。

正式启用前，在服务器内部隔离数据库上分别运行当前 Python `catalog_words` 与 Rust `englishctl catalog`，比较完整字节、SHA-256、账号范围和数据库身份/修订号；记录耗时、峰值 RSS 与产物哈希。真实数据库保持在服务器受限目录，不下载到本地。

## 启用

通过现有独立版本目录部署更新的 `backend/app/meaning_catalog.py`，保留原文件及 release。
创建 `/etc/systemd/system/englishlearning.service.d/native-catalog.conf`：

```ini
[Service]
Environment=ENGLISH_NATIVE_CATALOG=/opt/englishlearning/native/<commit>/englishctl
```

运行 daemon-reload 后重启应用。确认公开健康、版本和 Passkey 配置正常；在服务器内使用真实账户的受控测试检查冷目录、重复 304 与启动日志。不要为强制冷构建而删除用户的浏览器缓存或学习记录。

缓存锁仍在 Python 父进程持有，Rust 子进程只有只读连接。父进程核验输出文件大小和 SHA-256 后才原子发布，版本变化或失败时使用原 Python 路径。原有 v22、目录文件名、ETag 和账号隔离不变。移植的 FSRS 计算尚未接入线上学习。

## 回退

删除上述 drop-in、daemon-reload 并重启应用即可回到 Python 构建路径。Rust 执行失败时也会自动退回 Python，并记录不含用户内容的告警。若需回退本次应用代码，切回之前保存的 release，同时保留当前数据库和目录缓存。

故障时检查 `journalctl -u englishlearning.service` 中的 `Native catalogue unavailable`，以及二进制可执行权限、glibc、源库可读性、修订号和磁盘空间。缓存命中不会调用 Rust，不能用一次 304 作为原生构建已经运行的证据。
