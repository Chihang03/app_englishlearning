# Context Vocabulary Trainer

语境单词训练器。系统用英文例句挖空让用户根据上下文和当前义项的解释回忆单词，并用 SQLite 持久化保存学习记录与义项级 SRS 状态。支持多用户，每人有各自独立的学习进度。

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
    senses.py        # 义项、例句、旧进度迁移
    sense_learning.py # 按义项选卡、评分与统计
  data/
    seed_words.json  # 小型内置示例词库
    source_word_lists.json # 词表分类与词头快照
    vocabulary_catalog.json # 单词、义项及配套例句（format_version=2）
    vocabulary_export_report.json # 覆盖数、英文释义义项数、未配对词清单
    example_fallbacks.json # 本地化备用例句快照
  scripts/
    build_vocab_bundle.py # 仅在 Mac 上运行的一次性词典导出器
    dictionary_senses.py # 保留原词典中的义项/词性/例句关系
    sense_overrides.json # 人工核对的跨词典义项对应关系
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

首次启动后端时会自动创建 `$DATA_DIR/vocabulary.db`（默认 `backend/data/vocabulary.db`），并导入 `seed_words.json` 与本地化的 `vocabulary_catalog.json`。没有合格例句的源词不会导入为可学记录。后续学习记录、SRS 状态、词库选择和显示设置写入同一个 SQLite 数据库。

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
- `GET /api/settings` 获取显示设置和选中的词库
- `PATCH /api/settings` 更新显示设置和选中的词库
- `GET /api/word-lists` 获取可选词库及可学习数量
- `GET /api/dictionary/{word}` 查询服务器自带的本地词库
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

## 本地词库与 Mac 导出

学习词库分为两层：`source_word_lists.json` 只保存 NGSL、四/六级、托福、雅思、GRE 等词表的词头分类；`vocabulary_catalog.json` 保存可直接学习的本地释义、发音和例句。服务器只读取这些随项目部署的静态文件，不调用 Mac 词典，也不需要安装导出依赖。

在 Mac 上生成或更新本地词库时，需先安装希望使用的系统词典。默认会读取仓库内已本地化的词头清单；如果要刷新考试/用途词表，再传入包含源文件的 `--source-dir`：

```bash
cd backend
python3 -m venv .venv-vocab-export
.venv-vocab-export/bin/pip install -r requirements-vocabulary-export.txt
.venv-vocab-export/bin/python scripts/build_vocab_bundle.py
# 可选：从新的源快照重建词头分类
.venv-vocab-export/bin/python scripts/build_vocab_bundle.py --source-dir /path/to/wordlist-sources
```

构建器读取 Mac 已安装的英汉词典和 New Oxford American Dictionary，按原文义项分组提取词性、解释及例句。英汉词典中的同组数据优先；只有人工核对的映射或词性一致且例句完全相同的唯一匹配，才跨词典合并。`sense_overrides.json` 保存人工映射及预期英文解释，源解释变化时导出会要求重新核对。没有可靠中文对应的词使用原义项的英文解释，不拼接整词的中文释义。

每个可学义项都必须有实际词典例句。构建器排除带 sb/sth 的模板和过短示例；词典省略的句末标点会被补齐。支持词典索引中明确记录的常见变形，例如 address → addressed；每条例句保存 `target_form`，卡片挖空与评分采用句中词形。独立的 Tatoeba 备用句没有义项对应关系，因此保留快照供人工核对，不会自动配给某个意思。

设置页显示“可学词数 / 原词表词数”，未找到可靠配对的词列在 `vocabulary_export_report.json` 中。重新生成后部署 `source_word_lists.json`、`vocabulary_catalog.json` 和导出报告即可，服务器及访问者设备不需要 Mac 词典或导出依赖。

## 一词多义学习与进度迁移

每张卡只显示当前例句所属义项的解释和词性。答题后可展开“其他意思与用法”，查看它们的解释、例句和学习状态。展开后暂停自动跳题，方便阅读。已有多个例句的义项会按当前用户的练习历史轮换例句。

SRS 按 `(user_id, sense_id)` 独立记录：答对“地址”不会改变 address 的“处理”义项进度。到期义项先复习，然后学习所选词表中的新词和未学义项；同一个义项跨四级/六级共用进度。旧词的其他意思显示为“新义项”，不重复统计为新单词。首页分别显示单词数和义项数，“已掌握单词”要求该词全部可学义项进入间隔复习。

今日成功要求在一轮新的作答中，未看答案就正确拼写例句所需词形。答错或空答会显示答案；在原题纠正正确只记录练习，不推进 SRS。纠正后至少间隔 20 分钟才会重新出题（后端按整秒向上取整，最长多 1 秒）。其他到期复习优先，其次是已到时间的待巩固义项，再其次是新词；错词尚未到时间时继续学习新词，界面不显示等待时间或倒计时。再次答错会重新纠正、排到队尾并重新计时。离开页面不影响服务器保存的到期时间。如果所选词库已经没有其他可学题目，界面建议选择其他词库，后台在待巩固词到时间时静默重新加载。

每张卡片带有后端签发的 `attempt_id`，答题时原样传回。刷新或重新登录会恢复本轮是否已看过答案；待巩固队列跨设备和跨天保存。同一义项在账号当地日期内最多独立通过一次，重复提交已完成轮次返回 409，前端会重新加载当前题目。首页“今日成功单词”按独立通过的单词去重，同时显示成功义项数；通过一个义项不表示该词所有意思都已掌握。独立作答正确率只统计每轮揭示答案前的作答，答题次数仍包含纠正。

数据库 v6 升级前自动备份，保留既有复习计划和历史，把原有 `Learning` 义项加入待巩固队列。旧答题记录无法可靠判断是否用过提示，因此不补算为独立通过；旧记录仍保留在答题次数和累计进度中。升级后新词只有独立通过才进入间隔复习，旧词答错后重新独立通过才恢复未来复习安排。

设置中的学习词库仅显示词表名称。点击后进入词库详情，查看总单词、已学习和已掌握数量，在详情中切换“加入学习”；词库说明折叠在详情中。`GET /api/word-lists` 返回 `learned_word_count` 和 `mastered_word_count`：已学习包含已发出的题目、答题记录及旧学习进度，按词去重，答错也计入；已掌握要求该词的全部当前有效义项处于 `Mature`（长期熟记）。统计按账号隔离，跨词库共用的单词计入每个所属词库，取消词库选择不清除进度。总单词按当前有效单词统计，原表中未能导入的词不计入。

数据库 v5 升级前会生成备份，并保留旧的单词级 SRS 和全部答题历史。只有旧例句与新词库中的唯一义项完全匹配时，才把旧计划转给那个义项；其他意思保持未学习。无法匹配的旧记录继续保留在累计学词数中，界面提示需要确认具体义项。重新导出保留稳定义项标识，取消的义项归档，不删除其历史。

`GET /api/next` 的卡片包含 `sense_id`、`example_id`、`answer_form`。提交 `POST /api/review` 时须传入 `word_id`、`sense_id`、`example_id` 和 `user_answer`，后端校验三者属于同一可访问记录。仅有一个义项且只有一个例句时，兼容旧的 word_id-only 请求；多义或多例句请求缺少标识会返回 422。本地词典接口同时返回结构化 `senses`。

Mac 导出器的结构解析验证可在安装了导出依赖的环境中单独运行：

```bash
cd backend
.venv-vocab-export/bin/python -m unittest discover -s tests -p test_dictionary_export.py -v
```

当前 TOEFL、IELTS、GRE 分类来自 ECDICT 的社区考试标签，并非考试机构发布的官方封闭词表；NGSL/NAWL/TSL/BSL 是按通用、学术、TOEIC、商务等用途组织，不是 CEFR 难度级别。CET-6 词表包含四级基础词及六级增补词。

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
