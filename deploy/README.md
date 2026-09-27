# Passkey 免费测试域名

服务器使用 `16.76.139.180`，测试域名为 `english.16-76-139-180.sslip.io`。

访问地址：<https://english.16-76-139-180.sslip.io:8443/#home>

该域名由 sslip.io 根据名称中的 IP 自动解析，不需要购买域名或添加 DNS 记录。它依赖第三方免费 DNS 服务，适合测试；正式使用可换成自己管理的固定域名。变更 WebAuthn RP ID 后，旧通行密钥无法直接用于新 RP ID，应保留密码登录并重新添加通行密钥。

## 服务器配置

- `nginx-passkeys-test.conf` 对应 `/etc/nginx/sites-available/englishlearning-passkeys`，通过 `/etc/nginx/sites-enabled/englishlearning-passkeys` 软链接启用。
- `passkeys-test.env.example` 对应 `/etc/englishlearning-passkeys.env`，服务器文件权限为 `0600`。
- `passkeys-test-systemd.conf` 对应 `/etc/systemd/system/englishlearning.service.d/passkeys.conf`，在原有应用环境配置之后加载 Passkey 环境变量。

现有 `8443` 端口由 Nginx 通过 SNI 区分 IP 入口和域名入口，分别使用对应证书。`443` 端口上的其他服务保持不变。应用后端仍位于 `127.0.0.1:8000`，学习数据库仍由原来的 `DATA_DIR` 管理。

原 IP 地址可继续使用密码登录，Passkey 通过新域名创建和使用。两个来源的浏览器会话 Cookie 分开，登录同一个账号后访问同一份学习记录。本次服务器配置不部署工作区中其他未提交的代码。

## 证书和自动续期

使用服务器已有的 Snap Certbot（`/snap/bin/certbot`），证书名称为 `englishlearning-passkeys-test`：

```bash
sudo /snap/bin/certbot certonly --webroot \
  --webroot-path /var/www/letsencrypt \
  --cert-name englishlearning-passkeys-test \
  -d english.16-76-139-180.sslip.io \
  --deploy-hook '/usr/sbin/nginx -t && /bin/systemctl reload nginx'
```

证书路径为 `/etc/letsencrypt/live/englishlearning-passkeys-test/`，私钥只保存在服务器上，不提交到仓库。HTTP-01 验证由 Nginx 在公网 `80` 端口提供 `/.well-known/acme-challenge/`。保留该路径和端口才能续期。

沿用服务器现有的 `snap.certbot.renew.timer` 自动续期，续期成功后运行保存的 deploy hook，检查并重载 Nginx。

```bash
sudo systemctl status snap.certbot.renew.timer
sudo /snap/bin/certbot renew --cert-name englishlearning-passkeys-test \
  --dry-run --run-deploy-hooks --no-random-sleep-on-renew
sudo nginx -t
curl -f https://english.16-76-139-180.sslip.io:8443/api/health
curl -f https://english.16-76-139-180.sslip.io:8443/api/auth/passkeys/config
```

2026-09-27 已验证：域名 HTTPS 证书受信任，当前证书到期时间为 2026-12-25 23:06:05 UTC；续期定时器已启用，续期演练及 Nginx 重载成功。新域名的健康检查和 Passkey 配置接口正常，原 IP 入口仍可访问。

Chrome 中的临时虚拟认证器已通过该域名的 WebAuthn 创建和签名操作，登录挑战 Cookie 带有 Secure 和 HttpOnly 属性。验证没有创建生产账号或保存生产通行密钥，也没有使用真实生物识别设备。

## 原配置备份和停用

首次配置前的备份位于服务器 `/var/backups/englishlearning-passkeys-20260927T000414Z/`，仅 root 可访问。

如需停用测试入口，只移除新增的 Nginx 启用软链接和 systemd Passkey drop-in，再执行 `systemctl daemon-reload`、重启 `englishlearning.service`、检查并重载 Nginx。原 IP 入口配置不需要恢复或替换。测试域名证书可保留，以便再次启用。

真实 Touch ID / Face ID、密码管理器同步和不同设备上的登录仍需要在实际设备上确认。
