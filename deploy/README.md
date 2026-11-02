# 部署配置

当前生产使用 Rust 原生服务，构建、切换、备份和恢复步骤见 [Rust 发布手册](RUST.md)。项目文档导航位于 [AGENTS.md](../AGENTS.md)。

## 私有连接信息

开发机在仓库外维护 `~/.config/app_englishlearning/deploy.env` 和 `operations.md`。连接参数包括 DEPLOY_HOST、DEPLOY_USER、DEPLOY_SSH_KEY 和 PUBLIC_ORIGIN，字段模板见 [deploy.env.example](deploy.env.example)。私有目录权限设为 0700，文件设为 0600。

服务器在 `/etc/englishlearning-deploy.env` 配置 PUBLIC_ORIGIN，文件由 root 持有并设为 0600。发布脚本在开始变更服务前加载该配置。需要另一路径时设置 ENGLISH_DEPLOY_CONFIG。回退入口使用保存的服务配置恢复。

## HTTPS 与通行密钥

`nginx-passkeys-test.conf`、`passkeys-test.env.example` 与 `push.env.example` 使用示例域名。安装时在服务器私有配置中设置真实域名、来源和证书，保持 WebAuthn RP ID、合法 Origin 与 HTTPS 入口一致。现有站点的 RP ID 和推送密钥应持续保留。

证书通过服务器的 Certbot 定时续期，保留 ACME 验证路径，续期后检查并重载 Nginx。具体服务器的地址、证书记录和操作备份位置写入仓库外的 operations.md。

## 检查与维护

提交前运行 `python3 scripts/check_sensitive_content.py`，检查整个工作树的已跟踪文件使用 `--tracked`。首次设置 `git config core.hooksPath .githooks` 后，提交会自动执行检查。Git hook 为本地辅助检查，CI 同时扫描当前文件和推送涉及的历史。

敏感标识检查读取本机私有配置以识别当前部署地址及密钥路径，同时识别私钥正文和公开 IPv4 地址。命中时仅打印文件与行号。占位地址使用文档保留网段或 example.com。

历史清理会改变提交哈希，其他克隆应重新克隆或按新历史同步。清理前完整备份位于仓库外受限目录，不将备份重新导入或推送。
