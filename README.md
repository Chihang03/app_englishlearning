# Context Vocabulary Trainer

语境单词训练器。系统用英文例句挖空让用户根据上下文和中文释义回忆单词，并用 SQLite 持久化保存学习记录与 SRS 状态。支持多用户，每人有各自独立的学习进度。

## 技术栈

- Frontend: React, TypeScript, TailwindCSS, Vite
- Backend: Python FastAPI
- Database: SQLite（WAL 模式）
- Auth: 会话 Cookie + scrypt 口令散列 + WebAuthn 通行密钥（Passkey）
- TTS: 浏览器 Web Speech API（`speechSynthesis`）

## 目录

```text
backend/
  app/
    main.py          # FastAPI API + 生产环境静态前端托管
    auth.py          # 注册 / 登录 / 会话 / 改密
    passkeys.py      # 通行密钥注册、登录与管理
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
    PasskeySettings.tsx # 通行密钥管理
    passkeys.ts      # 浏览器 WebAuthn 调用
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
| `WEBAUTHN_RP_ID` | `localhost` | 通行密钥绑定的域名，不含协议、端口或路径，不能是 IP 地址。 |
| `WEBAUTHN_RP_NAME` | `Context Vocabulary Trainer` | 创建通行密钥时显示的服务名称。 |
| `WEBAUTHN_ORIGINS` | `http://localhost:5173,http://localhost:8000` | 逗号分隔的前端来源白名单，必须包含协议和实际端口，不含路径或末尾 `/`。正式环境必须 HTTPS；HTTP 仅允许 localhost。 |

注意 `seed_words.json` 始终随代码从仓库读取，不受 `DATA_DIR` 影响。

## 数据

首次启动后端时会自动创建 `$DATA_DIR/vocabulary.db`（默认 `backend/data/vocabulary.db`），并导入 `backend/data/seed_words.json` 中的示例单词。后续学习记录、SRS 状态和设置都会写入同一个 SQLite 数据库。

## API 摘要

- `GET /api/stats` 首页统计
- `GET /api/next` 获取下一题
- `POST /api/review` 提交答案并更新 SRS
学习与设置接口需要登录；健康检查、注册、登录及通行密钥登录流程公开。

- `POST /api/auth/register` 注册
- `POST /api/auth/login` 登录
- `POST /api/auth/logout` 登出
- `GET /api/auth/me` 当前账号
- `PATCH /api/auth/me` 修改时区
- `PATCH /api/auth/password` 修改密码（会使其他设备的登录失效）
- `GET /api/auth/passkeys/config` 通行密钥站点配置（公开）
- `GET /api/auth/passkeys` 当前账号的通行密钥列表
- `POST /api/auth/passkeys/register/options` 确认当前密码并获取注册选项（需要登录）
- `POST /api/auth/passkeys/register/verify` 验证并保存通行密钥（需要登录）
- `POST /api/auth/passkeys/login/options` 获取登录选项（公开）
- `POST /api/auth/passkeys/login/verify` 验证签名并建立原有 Cookie 会话（公开）
- `DELETE /api/auth/passkeys/{id}` 确认当前密码并删除自己的通行密钥
- `GET /api/settings` 获取显示设置
- `PATCH /api/settings` 更新显示设置
- `GET /api/dictionary/{word}` 尝试通过 macOS DictionaryServices 查询系统词典
- `POST /api/words` 新增单词
- `POST /api/words/import` 批量导入单词

## 通行密钥（Passkey）

首次使用先注册或用密码登录，再在账号区域打开「管理通行密钥」，输入当前密码、密钥名称，点击「添加通行密钥」。浏览器会调用设备或密码管理器完成确认。下次点击登录页的「使用通行密钥登录」，无需输入用户名和密码。每个账号支持最多 20 个密钥，可查看添加日期、最近登录时间并删除；删除时需要当前密码。删除服务端记录后，该密钥不能再登录，设备或密码管理器中的副本需要用户自行清理。

密码注册、密码登录和改密保持可用。建议设置可靠的备用密码，并添加备用通行密钥。当前版本没有邮箱找回或恢复码；如果所有通行密钥和密码都丢失，不能自助恢复。修改密码会使其他设备的会话失效，但不会删除已有通行密钥；怀疑密钥泄露时应在管理界面删除它。

本机开发请通过 `http://localhost:5173` 或 `http://localhost:8000` 访问。普通 HTTP 服务器地址或局域网 IP 不支持此登录方式，界面会显示原因并保留密码入口。浏览器支持 WebAuthn 不代表设备一定具有可用的通行密钥；用户取消、超时或没有密钥时仍可用密码登录。

正式部署先确定稳定 HTTPS 域名，并为后端设置，例如：

```bash
WEBAUTHN_RP_ID=learn.example.com
WEBAUTHN_ORIGINS=https://learn.example.com
COOKIE_SECURE=true
```

这些设置是后端进程的环境变量，本项目不会自动读取 `.env` 文件。反向代理提供 HTTPS，前后端仍可保持同源。RP ID 必须与前端域名相同或为其父域，服务器不会从请求的 Host 或转发头自动信任域名。变更 RP ID 后旧密钥不能直接用于新 RP ID，应保留密码入口并重新添加密钥。前后端分离时还需设置 `CORS_ORIGINS`，且会话 Cookie 仍受浏览器的同站策略约束，推荐同源部署。

服务器只保存公钥、凭据标识和必要元数据，不接收指纹或面容。注册及登录均要求用户验证；挑战绑定当前浏览器、来源和用途，注册挑战还绑定账号及会话，5 分钟过期且只能使用一次。设备同步能力取决于用户使用的系统或密码管理器。

## 认证测试

```bash
cd backend
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```

测试使用临时数据库及软件生成的签名，验证注册、登录、账号隔离、挑战过期和重放、用户验证和删除行为，不会修改运行中的学习数据库。真实 Touch ID / Face ID、密码管理器同步和正式域名仍需在实际设备上验证。

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
