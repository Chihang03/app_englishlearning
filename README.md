# Context Vocabulary Trainer

语境单词训练器。系统用英文例句挖空让用户根据上下文和中文释义回忆单词，并用 SQLite 持久化保存学习记录与 SRS 状态。支持多用户，每人有各自独立的学习进度。

## 技术栈

- Frontend: React, TypeScript, TailwindCSS, Vite
- Backend: Python FastAPI
- Database: SQLite（WAL 模式）
- Auth: 会话 Cookie + scrypt 口令散列（均来自标准库，无额外依赖）
- TTS: 浏览器 Web Speech API（`speechSynthesis`）

## 目录

```text
backend/
  app/
    main.py          # FastAPI API + 生产环境静态前端托管
    auth.py          # 注册 / 登录 / 会话 / 改密
    security.py      # 口令散列、会话令牌、时区处理
    migrations.py    # 版本化 schema 迁移
    database.py      # 连接管理与查询
    srs.py           # SRS scheduling
    dictionary.py    # macOS 系统词典查询（仅 macOS 可用）
  data/
    seed_words.json  # 初始示例词库（公共词库）
frontend/
  src/
    App.tsx          # 认证网关 + 训练界面
    AuthScreen.tsx   # 登录 / 注册
    api.ts           # fetch 封装与错误处理
    types.ts
    main.tsx
    index.css
```

## 运行

安装后端依赖：

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

安装前端依赖并启动：

```bash
cd frontend
npm install
npm run dev
```

然后打开 Vite 显示的本地地址，通常是 `http://localhost:5173`。

## 生产环境运行

生产环境下前端与后端同源：先构建前端，后端会自动把 `frontend/dist` 挂载到 `/`，只需要跑一个进程。

```bash
cd frontend && npm ci && npm run build
cd ../backend && uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开 `http://<host>:8000` 即可，API 仍在 `/api` 下。因为同源，默认不需要任何 CORS 配置。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `backend/data` | SQLite 数据库存放目录。部署时指向持久化卷，避免重新部署时丢数据。 |
| `STATIC_DIR` | `frontend/dist` | 构建好的前端目录。目录不存在时跳过挂载（开发模式即如此）。 |
| `CORS_ORIGINS` | 空 | 逗号分隔的来源白名单。仅在前后端不同源时才需要设置。 |
| `DEFAULT_TIMEZONE` | `Asia/Shanghai` | 未指定时区的账号使用的默认时区。 |
| `REGISTRATION_CODE` | 空 | 设置后注册必须提供该邀请码。想彻底关闭注册就设一个别人不知道的随机值。 |
| `COOKIE_SECURE` | `false` | 设为 `true` 后会话 Cookie 仅通过 HTTPS 发送。**部署到 HTTPS 后务必开启**；纯 HTTP 环境下开启会导致无法登录。 |
| `INITIAL_USERNAME` | `admin` | 首次迁移旧数据时创建的账号名，仅在迁移那一次生效。 |
| `INITIAL_PASSWORD` | 随机生成 | 同上。不设置则随机生成并在启动日志中打印一次。 |

注意 `seed_words.json` 始终随代码从仓库读取，不受 `DATA_DIR` 影响。

## 数据

首次启动后端时会自动创建 `$DATA_DIR/vocabulary.db`（默认 `backend/data/vocabulary.db`），并导入 `backend/data/seed_words.json` 中的示例单词。后续学习记录、SRS 状态和设置都会写入同一个 SQLite 数据库。

## API 摘要

- `GET /api/stats` 首页统计
- `GET /api/next` 获取下一题
- `POST /api/review` 提交答案并更新 SRS
除 `GET /api/health` 外，所有接口都需要登录。

- `POST /api/auth/register` 注册
- `POST /api/auth/login` 登录
- `POST /api/auth/logout` 登出
- `GET /api/auth/me` 当前账号
- `PATCH /api/auth/me` 修改时区
- `PATCH /api/auth/password` 修改密码（会使其他设备的登录失效）
- `GET /api/settings` 获取显示设置
- `PATCH /api/settings` 更新显示设置
- `GET /api/dictionary/{word}` 尝试通过 macOS DictionaryServices 查询系统词典
- `POST /api/words` 新增单词
- `POST /api/words/import` 批量导入单词

## macOS Dictionary

后端提供 `GET /api/dictionary/{word}`，优先尝试调用 macOS 的 `DictionaryServices` 本地框架。该框架是否能返回内容取决于当前系统已安装并启用的词典；如果系统不返回词条，接口会返回 `available: false`。学习用词条仍可通过 `seed_words.json`、`POST /api/words` 或 `POST /api/words/import` 导入，确保应用不依赖网络。

## 发音

发音由浏览器的 Web Speech API 完成，不依赖服务端，因此部署到 Linux 服务器上同样可用。语音和语速是每台设备各自的偏好，保存在浏览器 `localStorage`，不写入数据库。可选语音取决于访问者自己系统里装了哪些英文语音。

## 账号与数据归属

每个账号有独立的 SRS 进度、复习历史和显示设置。`seed_words.json` 里的词属于**公共词库**（`owner_id` 为 NULL），所有人可见；通过 `POST /api/words` 或 `POST /api/words/import` 添加的词归添加者私有，其他账号看不到也复习不到。

## 时区

每个账号有自己的时区（注册时由浏览器自动填入，可用 `PATCH /api/auth/me` 修改）。复习时间戳统一以 UTC 存储，"今天"、每日统计、连续天数和复习到期日都在账号自己的时区里计算。这样服务器跑在 UTC 也不会让身处东八区的用户日界线错位。

## 数据库迁移

启动时会自动把数据库升级到当前 schema 版本（用 SQLite 的 `user_version` 跟踪），**升级前会自动在同目录留一份 `.bak-v<版本>-<时间戳>` 备份**。

从单用户版本升级时，已有的学习记录会全部归属到一个新建账号：账号名取自 `INITIAL_USERNAME`（默认 `admin`），密码取自 `INITIAL_PASSWORD`；未设置 `INITIAL_PASSWORD` 时会随机生成并在启动日志里打印一次，登录后可在界面右侧「账号 → 修改密码」自行修改。

## 部署提醒

公网部署时至少要做到：走 HTTPS 并设置 `COOKIE_SECURE=true`；设置 `REGISTRATION_CODE` 以免被任意注册；把 `DATA_DIR` 指向持久化目录。
