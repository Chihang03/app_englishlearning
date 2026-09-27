# 浏览器缓存与更新

缓存策略由应用后端统一设置，IP 和 Passkey 域名入口都有效，不依赖单独修改 Nginx。

| 内容 | 策略 |
| --- | --- |
| `/assets/` 下带内容哈希的资源 | `public, max-age=31536000, immutable` |
| HTML、无版本文件 | `no-cache`，保留 ETag / Last-Modified 校验 |
| `/api/`、错误响应 | `no-store` |

静态文件使用 gzip；登录和个人数据接口不压缩。浏览器请求 API 时也明确跳过 HTTP 缓存。

首页统计在当前账号内复用 10 秒，设置和词库列表复用 30 秒。并发读取合并，修改操作完成后清空缓存；被修改操作失效的旧请求不能重新填入旧数据。手动刷新和复习到期刷新始终请求服务器。下一题、提示、更多词义和答题不走只读缓存。

离开前台至少 30 秒、从浏览器历史快照恢复或重新联网后，重新检查会话并刷新只读数据，保留当前题目与输入。网络错误不会被当成会话过期；只有 401 才进入登录。

构建产生 `dist/version.json`，`/api/version` 读取当前服务的构建版本。页面启动、恢复前台及可见时每 5 分钟检查版本。检测到新版后，仅在首页、没有未完成输入或操作时自动重新加载；学习页和设置页不自动刷新。这不是离线学习功能。

每次发布先运行 `npm run version:update`，再构建，保证 JS 中的版本号与 `dist/version.json` 一致。发布切换代码与完整的 dist 目录；保留上一版供回滚，学习数据库继续位于原 `DATA_DIR`。

验证：

```sh
cd frontend
node --test tests/cache.test.mjs
npm run build
cd ../backend
DATA_DIR=/tmp/englishlearning-cache-tests .venv/bin/python -m unittest discover -s tests -p test_http_cache.py
```

真实 iPhone 上还应确认长时间切后台后的恢复，以及连续答题过程中发布新版不会丢失输入。桌面浏览器和合成事件测试不能代替实际 iOS 生命周期验证。
