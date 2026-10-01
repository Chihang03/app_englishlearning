# 浏览器缓存与更新

缓存策略由应用后端统一设置，IP 和 Passkey 域名入口都有效，不依赖单独修改 Nginx。

| 内容 | 策略 |
| --- | --- |
| `/assets/` 下带内容哈希的资源 | `public, max-age=31536000, immutable` |
| HTML、无版本文件 | `no-cache`，保留 ETag / Last-Modified 校验 |
| `/api/`、错误响应 | `no-store` |

静态文件和足够大的 API 响应使用 gzip。浏览器请求 API 时明确跳过 HTTP 缓存。

首页统计在当前账号内复用 10 秒，设置和词库列表复用 30 秒，不再学习的单词列表复用 5 分钟。这四类显示数据在 IndexedDB 按账号保存快照；冷启动先显示快照，再核对服务器。后台读取失败时保留已有快照，401 与手动强制刷新错误仍向上层报告。

并发读取合并，写操作仅使关联数据失效，失效时同步删除该快照。下一题和答题影响统计与词库进度；语速、翻译显示设置只影响设置；词库选择、基础词过滤和静音操作影响各自关联资源。失败的写请求也会触发失效，因为服务器可能已经接受写入。失效之前的旧读取不能填回旧数据。下一题、提示和答题始终由服务器确认。

完整词义和例句目录保存在账号隔离的 IndexedDB 中，无固定到期删除时间。每 6 小时、应用版本变化及手动刷新时按内容 ETag 核对，未变化的目录保留；实际内容变化才替换。更多义项的完整词形信息另保存最近 50 个查看记录，保留 30 天。主页刷新先完成必要上传和概览读取，完整词义下载静默进行。

Service Worker 在所有受支持的入口注册，不依赖通知授权。生产构建预缓存 HTML、带哈希的 JS/CSS、图标和 manifest；安装下载失败时不会激活不完整版本。页面导航优先联网，网络失败或服务器 5xx 时回退到完整本地页面。API 和写请求不由 Service Worker 缓存或重放。开发模式不启用页面预缓存。

离线重开只使用上一次已验证学习账号的词义目录，提供搜索和义项浏览；本地身份记录不构成登录会话。恢复联网后先验证会话再进入学习。退出登录或收到 401 后清除离线身份，管理员不提供离线入口。IndexedDB 和 Cache Storage 均可能被设备清理，存储失败时在线功能继续工作，之后可重新下载。

离开前台至少 30 秒、从浏览器历史快照恢复或重新联网后，重新检查会话并刷新只读数据，保留当前题目与输入。网络错误不会被当成会话过期；只有 401 才进入登录。

构建产生 `dist/version.json`，`/api/version` 读取当前服务的构建版本。页面启动、恢复前台及可见时每 5 分钟检查版本。检测到新版后，仅在首页、没有未完成输入或操作时激活完整的新资源版本并重新加载；学习页和设置页不自动刷新。保留前一个资源缓存供已经打开的页面使用。离线浏览不支持离线答题。

每次发布先运行 `npm run version:update`，再构建，保证 JS 中的版本号与 `dist/version.json` 一致。发布切换代码与完整的 dist 目录；保留上一版供回滚，学习数据库继续位于原 `DATA_DIR`。

验证：

```sh
cd frontend
node --test tests/cache.test.mjs
npm run build
# 对本地临时数据库与词库运行浏览器回归：
APP_TEST_URL=http://127.0.0.1:18766 PLAYWRIGHT_MODULE=/path/to/playwright node tests/offline-cache.test.mjs
APP_TEST_URL=http://127.0.0.1:18766 PLAYWRIGHT_MODULE=/path/to/playwright node tests/manual-sync.test.mjs
APP_TEST_URL=http://127.0.0.1:18766 PLAYWRIGHT_MODULE=/path/to/playwright node tests/meaning-cache.test.mjs
# 资源更新失败测试须使用 /tmp 下的 dist 副本与独立测试服务：
APP_TEST_URL=http://127.0.0.1:18767 APP_TEST_DIST=/tmp/cvt-shell-test-dist PLAYWRIGHT_MODULE=/path/to/playwright node tests/shell-update.test.mjs
cd ../backend
DATA_DIR=/tmp/englishlearning-cache-tests .venv/bin/python -m unittest discover -s tests -p test_http_cache.py
```

真实 iPhone 上还应确认长时间切后台后的恢复，以及连续答题过程中发布新版不会丢失输入。桌面浏览器和合成事件测试不能代替实际 iOS 生命周期验证。
