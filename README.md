# Context Vocabulary Trainer

语境单词训练器。系统用英文例句挖空让用户根据上下文和当前义项的解释回忆单词，并用 SQLite 持久化保存学习记录与义项级 SRS 状态。支持多用户，每人有各自独立的学习进度。

## 技术栈

- Frontend: React, TypeScript, TailwindCSS, Vite
- Backend: Python FastAPI
- Database: SQLite（WAL 模式）
- Auth: 会话 Cookie + scrypt 口令散列 + WebAuthn 通行密钥（Passkey）
- TTS: 浏览器 Web Speech API（`speechSynthesis`）

设置页底部显示程序版本号，例如 `26.9.27.27`。每次提交 main 前，在 `frontend` 运行 `npm run version:update` 并将生成的 `src/version.json` 一起提交；脚本按北京时间生成日期，末位为 main 主线（first-parent）的累计提交数加一，不随日期变化重置。重新运行脚本不会额外递增，重新构建同一提交也不会改变版本号。

设置中的“跳过基础 600 词”按 NGSL 通用高频源词表的前 600 个词头过滤，默认关闭，每个账号独立保存。开启后，这些词的所有义项均退出新词、到期复习和待巩固队列，跨词库和同词头的历史私有词条均生效。同时跳过这些词的宾格、所有格、反身代词以及指示代词复数形式，例如 him、me、his、my、herself、these、those；按义项区分，mine 的代词义会跳过，独立名词义保留，不按词根扩展到派生词。新词、复习、待巩固、当前题目及进度统计使用相同规则。学习历史、SRS 和手动“不再学习”记录保留；关闭后恢复原有队列。它不修改例句，也不把跳过的词计为已掌握。

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
    srs.py           # 复习次数与错误计数
    adaptive_memory.py # FSRS、长期保持率与个人遗忘速度校准
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
- `GET /api/learning-calendar?month=YYYY-MM` 学习日历，省略月份时返回账户时区的当月；返回每日完成卡片、已记录学习时长、首次作答正确率及月度汇总。
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

用户自导入功能已停用。原 `POST /api/words` 和 `POST /api/words/import` 固定返回 `410 Gone`，不解析导入内容、不写入数据库，也不出现在 OpenAPI 文档中。公共词库仍由 Mac 字典离线导出并随项目部署。

词库 JSON 统一使用 UTF-8、两空格缩进和末尾换行，保留字段及词条顺序。导出和翻译导入脚本自动使用该格式；翻译批次的 `.jsonl` 继续每条记录一行。词库指纹忽略 JSON 排版空白，纯格式调整不会重新导入词库或使已有词形绑定失效。

```bash
python3 backend/scripts/format_vocab_json.py          # 格式化词库及人工规则
python3 backend/scripts/format_vocab_json.py --check  # 只检查格式
```

## 管理员账号

`admin` 是预留用户名，普通注册无法使用（不区分大小写）。管理员从原登录页登录后直接进入管理概览，查看用户总数、今日学习人数、累计与今日作答及公共词库单词数。「用户」入口进入分页列表，点击账号可查看注册时间、时区、作答与学习统计、通行密钥数量和所选词库。「待处理反馈」是独立入口，可筛选待处理/已处理反馈、查看问题与原题目、保存处理备注、标记已处理及重新打开。概览的今日统计按管理员时区计算，用户详情的今日作答按该用户时区计算。管理接口仅管理员可访问；管理员不进入学习流程，仍可修改密码、管理通行密钥和退出登录。

数据库 v10 为现有账号增加 `learner` 角色，保留原有学习记录，不自动把同名学习账号升级为管理员。在应用完成数据库初始化和升级后，通过服务器控制台创建管理员：

```bash
cd backend
DATA_DIR=/path/to/persistent-data .venv/bin/python scripts/create_admin.py
```

脚本交互输入初始密码，只保存口令散列。重复运行不会重设已有管理员的密码；遇到同名学习账号会停止并保留该账号。普通注册请求不能指定管理员角色。

数据库 v11 为反馈增加处理备注、处理时间及处理管理员；v12 保存内容修正和修改记录。在反馈详情点击「修正内容」可修改对应义项的中英文释义、例句、翻译、答案词形和音标；「保存修正并处理」会更新实际学习内容并将反馈标为已处理。原题目快照保留，词、义项及例句 ID 保持不变，原学习历史与 SRS 保留。受影响的未完成题目会结束，下次取题使用修正后的内容。答案词形必须存在于例句中，冲突或已归档题目会拒绝更新。

内容修正保存在持久化数据库中，重启或重新导入同一义项/例句的源词库时继续应用，保持例句 ID；上游删除的义项/例句仍归档。音标修改影响音标文本，浏览器的实际朗读声音仍由语音设置决定。管理页可查看提交时内容及最近十次修改记录。

管理接口：`GET /api/admin/overview`、`GET /api/admin/users?offset=0&limit=50`、`GET /api/admin/users/{id}`、`GET /api/admin/reports?status=pending&offset=0&limit=50`、`GET /api/admin/reports/{id}`、`PATCH /api/admin/reports/{id}`、`PATCH /api/admin/reports/{id}/content`。列表每页最多 100 条；响应不包含密码、会话令牌或通行密钥凭据。

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

新词在所选词库范围内按通用英语词频从高到低出现，保留全新单词优先于已学词的新用法的规则。同频词使用固定的打散顺序，同词内部保留学习单元顺序；未收录或只有多词组合估算的词频记为缺失，排在有词频的词之后，仍可学习。已发出的题目、到期复习、错词巩固、账号隔离、手动“不再学习”和“跳过基础 600 词”继续使用原来的规则；基础 600 词仍按 NGSL 原始源排名确定。

`word_frequencies.json` 是独立的词头词频快照，采用 `wordfreq==3.1.1` 的英语 large 词表。Zipf 分数以百分之一的整数保存，来源是约截至 2021 年的多语料通用使用频率，不代表考试题中的出现频率或某个具体义项的频率。数据和导出报告内嵌来源、版本及 CC BY-SA 4.0 署名/许可说明。`word_frequency_report.json` 列出各词库覆盖率、缺失词和按新顺序排列的前 50 个词。服务器只导入该静态快照，不安装或调用 wordfreq。

数据库 v19 升级前自动备份，只增加 `word_frequencies` 和 `word_frequency_state` 表。词频按规范化词头共享，不重建词库或改写单词、义项、学习单元和用户进度；快照独立更新，不影响词库内容指纹。

更新公共词库后，在独立导出环境重新生成词频快照与报告：

```bash
cd backend
python3 -m venv .venv-word-frequency-export
.venv-word-frequency-export/bin/pip install -r requirements-word-frequency-export.txt
.venv-word-frequency-export/bin/python scripts/export_word_frequencies.py
```

验证时显式设置临时 `DATA_DIR`，运行 `tests/test_word_frequencies.py`；发布前在生产数据库的 SQLite backup 副本上预演迁移与启动，核对全部原有表的内容和外键完整性。

在 Mac 上生成或更新本地词库时，需先安装希望使用的系统词典。默认会读取仓库内已本地化的词头清单；如果要刷新考试/用途词表，再传入包含源文件的 `--source-dir`：

```bash
cd backend
python3 -m venv .venv-vocab-export
.venv-vocab-export/bin/pip install -r requirements-vocabulary-export.txt
.venv-vocab-export/bin/python scripts/build_vocab_bundle.py
# 可选：从新的源快照重建词头分类
.venv-vocab-export/bin/python scripts/build_vocab_bundle.py --source-dir /path/to/wordlist-sources
```

`word_forms.json` 保存按词头和词性区分的词典词形信息。构建器同时导出该文件，也可单独运行 `scripts/export_word_forms.py` 更新词形，不重建学习例句。它读取词典屈折字段中的过去式、过去分词等标签，并用实际索引词形补齐规则变化；短语和派生词不会直接作为屈折词形导出。

构建器读取 Mac 已安装的英汉词典和 New Oxford American Dictionary，按原文义项分组提取词性、解释及例句。英汉词典中的同组数据优先；只有人工核对的映射或词性一致且例句完全相同的唯一匹配，才跨词典合并。`sense_overrides.json` 保存人工映射及预期英文解释，源解释变化时导出会要求重新核对。没有可靠中文对应的词使用原义项的英文解释，不拼接整词的中文释义。

`backend/data/chinese_gloss_supplements.json` 保存中文补充释义及每条记录的来源、匹配方法、稳定义项键、预期英文解释和词性；只填缺失的中文，重建词库时继续应用。`scripts/supplement_chinese_from_dictionary.py` 从本地词典提取补充：词性须一致，英文解释或例句须完全匹配；另可匹配两部原词典在该词性下都只有一个义项的单义词。单义判断包含原词典中没有可学例句的义项，不只检查学习词库。无法确定对应关系的多义词继续保留英文。词典义项中的复数/变体提示不会再被误当成短语而跳过整组解释。

加入 `--include-traditional` 可同时读取 Mac 已安装的译典通英汉双向字典，并用 macOS 自带的字符转换转为简体。`dictionary_gloss_exclusions.json` 记录已发现的跨词典同形异义，防止重建时再次误配。补充提取可以从仓库根目录单独运行，不会重建义项或例句：

```bash
backend/.venv-vocab-export/bin/python backend/scripts/supplement_chinese_from_dictionary.py \
  --catalog backend/data/vocabulary_catalog.json \
  --supplements backend/data/chinese_gloss_supplements.json --include-traditional
```

已有 201 条对话模型核对的补充释义保留，本次新补充直接取自词典，不调用翻译模型。原英文解释、例句、义项标识和学习进度保留。`scripts/apply_chinese_glosses_to_db.py` 用于线上增量更新：先通过 SQLite backup API 备份，再校验旧词库指纹，只更新缺失的公共中文释义和新指纹，并逐表核对其他内容及学习记录未变。

每个可学义项都必须有实际词典例句。构建器排除带 sb/sth 的模板和过短示例；词典省略的句末标点会被补齐。支持词典索引中明确记录的常见变形，例如 address → addressed；每条例句保存 `target_form`，卡片挖空与评分采用句中词形。独立的 Tatoeba 备用句没有义项对应关系，因此保留快照供人工核对，不会自动配给某个意思。

设置页显示“可学词数 / 原词表词数”，未找到可靠配对的词列在 `vocabulary_export_report.json` 中。重新生成后部署 `source_word_lists.json`、`vocabulary_catalog.json`、`word_forms.json` 和导出报告即可，服务器及访问者设备不需要 Mac 词典或导出依赖。

## 一词多义学习与进度迁移

### 动态记忆与长期熟记

复习以用户和义项为单位使用 `fsrs==6.3.0`。模型根据实际经过时间及独立作答结果更新记忆难度和稳定性，再计算约 80% 目标回忆概率对应的间隔；不再用固定阶梯决定后续日期。模型按 UTC 时间更新，复习日期仍按账号当地日历安排。原有到期日期在升级时保留，到下一次真实作答时才初始化记忆状态，旧间隔仅作为初始估计。

首次独立答对与后续复习都直接使用 FSRS 更新稳定性和难度，不使用 30 天熟词先验、30/60 天间隔上限或三次熟词确认路径。已有记忆状态和到期日期保留，下一次真实作答开始按新规则计算；旧熟词标志不再参与出题、间隔和界面显示。模型版本为 `fsrs-6.3.0-retention80-v2`，不修改数据库结构或重写历史答案。

长期熟记以未来 180 天为判断窗口：本次独立答对后，按新的记忆状态计算 `P_FSRS(180天) ** forgetting_multiplier`，达到 80% 即进入 `Mature`。不再叠加三次确认、首次至今至少 90 天、稳定性至少 180 天或最近 180 天无错误的门槛。FSRS 的 stability 仍定义为基础回忆概率下降到 90% 的天数，因此不能直接把 stability 当作 80% 的长期熟记门槛。首次答对后当下接近 100% 的概率也不用于判定长期熟记。

新模型判为 `Mature` 的学习单元每 180 天抽查，抽查到期与普通到期复习使用同一统计和选卡规则。失败或需要提示后重新学习，单词全部当前有效学习单元成熟才计为长期熟记单词。升级前已成熟且没有模型状态的记录保留原计划，不自动加入抽查或重新估计。

答题前朗读单词先通过 `POST /api/study/hint` 持久化提示记录，刷新或跨设备后仍生效。听提示后答对只记为辅助作答，不增加独立通过数，也不作为训练标签，至少 20 分钟后再次独立巩固。打开“其他意思与用法”前，通过 `POST /api/study/related-exposure` 记录相关义项的答案暴露，近期预览过的义项按辅助作答处理。答案纠正不再次更新 FSRS。前端记录最多五分钟的活动作答耗时，页面隐藏、失焦或超过 30 秒无键盘/指针活动的时间不计入；这是辅助遥测，不用速度直接判定熟练。

每次真正延迟至少 24 小时、无提示的首答保存作答前的基础回忆概率、个人校准概率和模型版本。旧记录以及同轮纠正不补造预测。个人校准学习一个有界的遗忘速度系数：`P_personal = P_FSRS ** multiplier`，同时将其用于求解下次日期。它是 FSRS 上的单参数机器学习校准，并非深度神经网络，也没有修改 FSRS 的 21 个权重。

校准自动在答题后触发：至少 150 个有效样本、20 个义项、跨度 30 天，之后每新增至少 50 个样本重新评估，最近最多 5000 个样本按时间顺序以前 80% 拟合、后 20% 验证。只有验证集平均对数损失比默认模型和现有个人模型都改善至少 0.005 时才采用；否则保留原参数。如果原个人模型比默认模型明显差，则回退到默认参数。门槛是本应用的保守产品策略。拟合只用过去的在线预测和真实结果，不使用当前答案计算当前预测；改动只影响未来实际作答后的计划，不批量重排待复习词。

`GET /api/stats` 的 `memory_model` 返回样本量、校准状态和验证损失；设置页简要说明自动复习。数据不足时依然动态更新单词记忆状态，只使用默认人群参数。实际长期记忆收益需要后续真实延迟作答验证，时间模拟测试不能证明真人记忆效果。

学习页面不显示下次复习日期、确认次数文字、纠正成功、稍后复习或重新输入的提示。已移除熟词确认的三条短横线，不增加替代说明文字。答错仅把填空处标红，修改答案和恢复题目时仍保持红色，答对后显示正确状态；输入框通过 `aria-invalid` 标记错误。网络错误与空题目时的词库选择建议保留；提示记录、校准和复习时间仍由后台照常处理。

运行后端验证：`cd backend && .venv/bin/python -m unittest discover -s tests -v`；前端验证：`cd frontend && npm run build`。v7 升级前自动用 SQLite backup API 备份，保留 WAL 中尚未检查点的数据。

每张卡只显示当前例句所属义项的解释和词性。学习页右上角的三点菜单提供“更多词义”和“报告错误”，左上角返回首页；首页通过底部导航进入设置。答题前后都可以打开“更多词义”，查看词库收录的解释与例句，当前义项会标记为“当前词义”；只有一个义项时明确提示。打开菜单或弹窗时暂停自动跳题，答对后可通过菜单的“下一题”继续。未分组的例句按账号和学习单元固定。已审核且启用轮换的组，在独立通过后的到期复习可换一个语境；同一轮刷新、纠错和错题巩固保持同一句。新义项优先选择答案与词头相同的原形例句；没有原形例句时使用该义项已有的例句。原句被词库归档后才选择并固定一个有效替代句。词性旁显示非原形提示，包括第三人称单数、过去式、过去分词、复数、比较级和最高级。-ing 答案结合固定例句，在明确结构下细化为动名词、现在分词或现在分词·进行时；不确定或多处挖空用法不同的句子保留 -ing 形式。

`POST /api/study/meanings` 使用当前 `attempt_id` 加载词义，在返回内容前持久化当前题目的答案暴露和其他义项的预览记录。答题前查看词义后，后续正确作答沿用辅助作答规则，不计为独立通过。报告错误只显示挖空例句，不揭示答案或改变学习进度。

错误报告可选择单词释义、例句、句子翻译、发音或其他问题，并填写最多 2000 字的说明。`POST /api/content-reports` 要求登录并校验题目归属，保存报告者、单词、义项、例句、内容快照和创建时间到数据库 v8 的 `content_reports` 表。同一用户对同一轮题目、同一类型的重试不会重复创建报告。界面只有收到服务器确认才显示提交成功；报告等待人工核对，不自动修改词库。管理员可在服务器数据库中查询 `SELECT id, category, details, content_snapshot, created_at FROM content_reports WHERE status='pending' ORDER BY created_at`。v8 升级前沿用 SQLite backup API 自动备份，原学习记录和计划保持不变。

### 错误报告审核接口

管理员可将待处理报告交给对话模型核对。导出包含问题类型、反馈说明、当前词义、例句、中文翻译、答案词形与必要的原题快照；不包含账号资料或登录信息。已有提交报告接口和管理员网页继续使用同一份数据。

| 接口 | 作用 |
| --- | --- |
| `GET /api/admin/report-review/export?limit=100&after_id=0` | 导出待处理报告，返回 `manifest`、`prompt` 和 `result_schema` |
| `POST /api/admin/report-review/validate` | 核对审核结果并预览处理计划 |
| `POST /api/admin/report-review/import` | 默认只预览；添加 `?dry_run=false` 才实际导入 |

三个接口均要求管理员登录 Cookie。`validate`、`import` 请求体为 `{"manifest":导出清单,"result":模型返回结果}`。普通学习账号不能读取其他用户的报告或修改公共词库。

模型返回格式如下，`batch` 使用导出清单中的值，必须逐条返回全部报告编号：

```json
{
  "batch": "导出清单中的batch",
  "items": [
    {
      "id": 123,
      "action": "correct",
      "changes": [{"field": "translation_cn", "value": "她被认为是个天才。"}],
      "notes": "保留 account 在此义项中表示认为、视为的含义。"
    }
  ]
}
```

`correct` 提出修正；`no_change` 确认内容正确；`manual` 表示无法判断并保留待处理状态。后两种的 `changes` 必须为空数组。可修改字段为 `definition_cn`、`definition_en`、`sentence`、`translation_cn`、`target_form`、`pronunciation`。没有实际音频时，模型不能凭文本认定朗读正确，提示词要求留待人工核对。

也可通过 `backend/scripts/report_exchange.py` 使用文件批次：

```bash
# 在可访问数据库的环境导出；默认每批 20 条、最多 100 条。
backend/.venv/bin/python backend/scripts/report_exchange.py export \
  --db /path/to/vocabulary.db --output /tmp/report-review

# 只生成审核建议，默认 gpt-6-luna、low；不会修改学习数据。
backend/.venv/bin/python backend/scripts/report_exchange.py run \
  --directory /tmp/report-review

# 结合当前数据库校验结果和预览处理计划。
backend/.venv/bin/python backend/scripts/report_exchange.py validate \
  --db /path/to/vocabulary.db \
  --manifest /tmp/report-review/batch-0001.manifest.json \
  --result /tmp/report-review/batch-0001.result.json

# 预览导入；实际处理时删除 --dry-run，admin-id 使用已有管理员账号 ID。
backend/.venv/bin/python backend/scripts/report_exchange.py import \
  --db /path/to/vocabulary.db --admin-id 1 --dry-run \
  --manifest /tmp/report-review/batch-0001.manifest.json \
  --result /tmp/report-review/batch-0001.result.json
```

导出和导入不调用模型。`run` 沿用现有 Codex CLI 登录，使用只读沙箱和临时会话，已完成批次会跳过，无需单独填写 API 密钥。批次可以复制到装有 Codex CLI 的电脑审核，再把结果交回服务器；服务器不必安装 Codex。

实际导入前自动备份 SQLite。导入会校验批次、报告归属及导出后的内容变化；互相冲突的建议、缺失编号、非法答案词形和已被更新的内容会拒绝处理，整批回滚。修正沿用管理员网页的持久化覆盖和修改记录，保留原题快照、义项及例句 ID、学习历史和复习计划。受影响的未完成题目按现有规则结束，下次取题使用修正内容。相同结果重复导入不会重复记录修改。

SRS 按 `(user_id, sense_id)` 独立记录：答对“地址”不会改变 address 的“处理”义项进度。到期义项先复习，然后学习所选词表中的新词和未学义项；同一个义项跨四级/六级共用进度。旧词的其他意思显示为“新义项”，不重复统计为新单词。首页分别显示单词数和义项数。学习概览显示“长期熟记”，要求单词全部当前有效学习义项达到 Mature；更多学习数据显示“已进入间隔复习”，按单词计数，要求该词全部可学义项进入间隔复习。

今日成功要求在一轮新的作答中，未看答案就正确拼写例句所需词形。答错或空答会显示答案；在原题纠正正确只记录练习，不推进 SRS。纠正后至少间隔 20 分钟才会重新出题（后端按整秒向上取整，最长多 1 秒）。其他到期复习优先，其次是已到时间的待巩固义项，再其次是新词；错词尚未到时间时继续学习新词，界面不显示等待时间或倒计时。再次答错会重新纠正、排到队尾并重新计时。离开页面不影响服务器保存的到期时间。如果所选词库已经没有其他可学题目，界面建议选择其他词库，后台在待巩固词到时间时静默重新加载。

每张卡片带有后端签发的 `attempt_id`，答题时原样传回。刷新或重新登录会恢复本轮是否已看过答案；待巩固队列跨设备和跨天保存。同一义项在账号当地日期内最多独立通过一次，重复提交已完成轮次返回 409，前端会重新加载当前题目。首页和学习页显示“今日完成卡片”（`today_completed_cards`）：按当天最终答对的轮次去重，首次独立答对、纠正后答对和辅助后答对均计一张；同一轮连续答错不增加，同一义项之后的新轮次可再次计数。未答题就停止学习或因词库更新而结束的轮次不计完成，旧版没有轮次标识的正确记录按每条记录计一次。总提交次数和独立通过单词/义项数保留在后台，普通用户界面不显示。独立作答正确率仍只统计每轮揭示答案前且没有辅助的作答。

累计学过、已学义项、进入间隔复习的义项、已进入间隔复习和长期熟记均排除“跳过基础 600 词”和“不再学习”的单词，与当前学习和复习范围保持一致；取消排除后重新计入，历史答题与复习计划保留。取消某个词库的选择仅影响新词，已开始的单词继续复习并计入进度。“已进入间隔复习”指一个单词全部当前有效学习义项处于 Reviewing 或 Mature，因此与“学习中的单词”可能重叠，不代表长期熟记。

20 分钟巩固间隔与后续 SRS 间隔一样，未到时间不计入待复习。首页以“即刻复习”（`due_words`）显示当前已到期且有可用例句的单词数，包括逾期复习和等待已结束的待巩固任务；同一单词的多个到期学习单元只计一个单词，出题仍按学习单元逐个完成，先处理全部到期任务再发新词。后台 `due_senses` 和卡片剩余复习数保留学习单元计数。不再重复显示学习中今日到期、待巩固单词/义项和待复习错词。到期时界面静默刷新统计，不显示等待时间。“学习中单词”（`learning`）按单词去重：至少有一个当前可学习且未长期熟记的学习单元已经答对过（包含纠正或辅助后答对），并且这个单词没有任何当前到期任务；独立答对后进入间隔复习仍属于学习中。只答错且从未答对的词不计入；一旦任何义项到期，整个单词只显示在即刻复习中，全部到期任务处理完后，尚未长期熟记的词再计入学习中。两栏互斥，沿用账号时区及跳过/不再学习规则。详细的到期子类计数仍由后台保留。

数据库 v6 升级前自动备份，保留既有复习计划和历史，把原有 `Learning` 义项加入待巩固队列。旧答题记录无法可靠判断是否用过提示，因此不补算为独立通过；旧记录仍保留在答题次数和累计进度中。升级后新词只有独立通过才进入间隔复习，旧词答错后重新独立通过才恢复未来复习安排。

设置中的学习词库仅显示词表名称。点击后进入词库详情，查看总单词、已学习和已掌握数量，在详情中切换“加入学习”；词库说明折叠在详情中。`GET /api/word-lists` 返回 `learned_word_count` 和 `mastered_word_count`：已学习包含已发出的题目、答题记录及旧学习进度，按词去重，答错也计入；已掌握要求该词在对应词库覆盖的全部当前有效学习单元处于 `Mature`（长期熟记）。统计按账号隔离，跨词库共用的单词计入每个所属词库，取消词库选择不清除进度。总单词按当前有效单词统计，原表中未能导入的词不计入。

数据库 v5 升级前会生成备份，并保留旧的单词级 SRS 和全部答题历史。只有旧例句与新词库中的唯一义项完全匹配时，才把旧计划转给那个义项；其他意思保持未学习。无法匹配的旧记录继续保留在累计学词数中，界面提示需要确认具体义项。重新导出保留稳定义项标识，取消的义项归档，不删除其历史。

`GET /api/next` 的卡片包含 `sense_id`、`example_id`、`answer_form`。提交 `POST /api/review` 时须传入 `word_id`、`sense_id`、`example_id` 和 `user_answer`，后端校验三者属于同一可访问记录。仅有一个义项且只有一个例句时，兼容旧的 word_id-only 请求；多义或多例句请求缺少标识会返回 422。本地词典接口同时返回结构化 `senses`。

Mac 导出器的结构解析验证可在安装了导出依赖的环境中单独运行：

```bash
cd backend
.venv-vocab-export/bin/python -m unittest discover -s tests -p test_dictionary_export.py -v
```

当前 TOEFL、IELTS、GRE 分类来自 ECDICT 的社区考试标签，并非考试机构发布的官方封闭词表；NGSL/NAWL/TSL/BSL 是按通用、学术、TOEIC、商务等用途组织，不是 CEFR 难度级别。CET-6 词表包含四级基础词及六级增补词。

## 发音

发音由浏览器的 Web Speech API 完成，不依赖服务端，因此部署到 Linux 服务器上同样可用。语速提供慢（90）、正常（120）、快（175）三档，默认正常；沿用旧的 175 基准换算浏览器语速倍率。选择立即生效，按账号缓存到浏览器 `localStorage` 并自动写入服务端设置，刷新或重新进入后恢复。保存失败时保留待同步选择，恢复网络或再次进入后重试；快速切换按顺序保存，加载中的旧值不会覆盖新的选择。英文语音仍按设备保存在浏览器中，可选声音取决于设备安装的英文语音。

## 账号与数据归属

### 词形与词族

学习和复习按学习单元保存，原始词典义项继续用于配对例句和查询。`lexical_units` 区分词汇身份，`word_forms` 保存有作用域和来源证据的屈折形式，`lexical_relations` 保存派生或词汇化关系。`say/says/said/saying` 的已审核动词形式查询复用 say 的原义项；saying 的名词“格言”及 absolute/absolutely 保持独立学习。更多词义弹窗分别展示屈折形式、派生词和词汇化用法，查看时记录实际返回内容的曝光。

`backend/data/word_forms.json` v2 保留原提示投影，并增加源词典指纹、身份、词形、关系和 saying 的真实词典补充义项。导出现在扫描整个词库的 12,892 个词头，已覆盖 12,830 个词头的来源身份；包含 17,422 条词形记录（含类型和义项作用域，对应 9,205 组原词—词形关联）及 4,348 条关系。37 个人工核对身份只是补充，不再限制自动提取范围。

`dictionary_morphology.py` 仅从真实词条的 POS 区块、`infg`、语法交叉引用和 `DERIVATIVES` 提取关联，不将搜索别名或词源段落当作屈折证据。规则只能给已明确列出的形式或精确匹配的真实例句目标标注类型；例句证据限定到原 sense，不能扩大到其他词义。多条同 POS 词源身份不能确定时，要求原义项精确作用域或跳过。无可靠证据的拼写及旧私有词保持 UNKNOWN。派生词保留独立复习，4,436 个派生身份只有关系元数据，没有真实可用例句就不新建学习内容。

旧复习历史通过原 `sense_id` 和词汇身份接入新版，不重建或清空记录，保留熟练度、成熟状态、间隔及固定例句；两个已经学习过的义项仍各自保留进度，词族不传递成熟状态。v14 增加身份元数据；v15 仅重建关系类型约束以支持派生动词，保留关系 ID 和所有原行。迁移前自动备份。

从安装的 Mac 词典重新导出全库证据（需要导出环境中的 lxml）：

```bash
python3 backend/scripts/build_morphology_bundle.py
```

`backend/scripts/morphology_overrides.json` 保存人工核对补充及原始记录 SHA-256，通用提取不依赖该名单。已审核记录变化会中止构建并要求重新审核；自动证据保留原记录指纹；catalog 指纹不匹配时运行端停用归并证据。完整词库构建及独立词形导出也会重建该证据。`backend/tests/test_morphology_export.py` 验证空审核名单也能提取词形、同词性同形词的范围和无例句派生词，`backend/tests/test_morphology.py` 验证分类、旧历史接入和迁移。

每个账号有独立的 SRS 进度、复习历史和显示设置。`seed_words.json` 和 `vocabulary_catalog.json` 的词属于**公共词库**（`owner_id` 为 NULL），所有人可见。当前不提供用户新增或批量导入词汇的能力；历史私有词条及其义项、复习进度和学习记录保留，继续按原账号隔离。

## 时区

每个账号有自己的时区（注册时由浏览器自动填入，可用 `PATCH /api/auth/me` 修改）。复习时间戳统一以 UTC 存储，"今天"、每日统计、连续天数和复习到期日都在账号自己的时区里计算。这样服务器跑在 UTC 也不会让身处东八区的用户日界线错位。

主页“总学习时长”汇总当前账号所有答题记录的 `active_response_ms`，包含答错和辅助作答。沿用已有的前台有效作答计时，不统计后台、长期闲置或尚未提交的时间；未记录耗时的旧答题不补估。停止学习某个词、跳过基础词或切换词库不扣除历史耗时。`GET /api/stats` 以毫秒返回 `total_study_time_ms`。

## 数据库迁移

学习页右上角菜单中的「不再学习此单词」对当前账号下的整个单词生效，包括全部义项、跨词库的同词和后续公共词库更新中的同词。操作后进入下一题，新词、复习、错题巩固和待学计数都会排除该词；历史作答和原有学习进度保留，不额外记作完成或掌握。可以通过操作后的「撤销」或设置中的二级页面「不再学习的单词 → 恢复学习」恢复原有安排。该页面按 A–Z 分组，列表使用屏幕可用高度，右侧首字母索引支持点击、拖动和键盘定位。schema v9 新增账号级消音表，升级前沿用 SQLite backup API 自动备份。

启动时会自动把数据库升级到当前 schema 版本（用 SQLite 的 `user_version` 跟踪），**升级前会自动在同目录留一份 `.bak-v<版本>-<时间戳>` 备份**。

从单用户版本升级时，已有的学习记录会全部归属到一个新建账号：账号名取自 `INITIAL_USERNAME`（默认 `admin`），密码取自 `INITIAL_PASSWORD`；未设置 `INITIAL_PASSWORD` 时会随机生成并在启动日志里打印一次，登录后可在界面右侧「账号 → 修改密码」自行修改。

## 学习单元和词库范围

schema v17 增加 `learning_units`、`learning_unit_senses`、`word_list_learning_units` 和 `word_list_learning_scopes`，区分原始词典解释、教学分组和具体词库范围。每个学习单元属于一个 `lexical_unit_id`，每个原始义项只归入一个组。`learning_enabled` 仍是全局适学开关；词库范围独立配置。

`backend/data/learning_units.json` 保存审核后的稳定分组标识、预期英文释义和范围来源。首批仅审核 abandon：遗弃某人、弃置某地、任其陷入某种处境、放弃计划归为“放弃；抛弃；遗弃”，四级只新增该组。中止活动、放任自己沉溺和名词“放纵”继续独立保留并可查询。其他单词暂时一义项一单元，保留原词表范围，后台标记 `legacy`；这不代表已经完成官方考试义项审核。明确审核为空的词库范围也能保存，后续新增义项不会自动扩展已审核范围。

schema v18 为学习状态、记忆模型、待巩固队列和固定例句增加 `learning_unit_id`；沿用原表名和来源锚点作为兼容字段，唯一索引保证每账号每学习单元只有一份活跃状态。旧来源行用 `retired_at` 归档。发题、答题、同日通过限制、提示曝光、记忆校准与学习统计均按单元处理；答题继续严格校验原始 `word_id + sense_id + example_id`，同时接受并校验新的 `learning_unit_id`，兼容旧客户端。

课程范围仅控制新增学习。已有复习不受取消词库或范围缩小影响；账号消音、基础词跳过和全局适学开关继续作用于新词、复习和巩固。多个词库覆盖同一单元时共用进度。词库“已掌握”按本词库覆盖范围计算，原始七个词典解释不会变成七份重复任务。

迁移前自动备份。分组保存 `learning_unit_merges` 归并凭据，包含原映射、学习状态、记忆曲线、固定句、队列、曝光和未完成轮次；原始历史 ID、词典义项 ID、例句 ID、作答时间和答案不改写。单边进度承接原曲线；多边冲突优先保留待巩固和较早到期，熟练度不相加、记忆稳定性不放大，确认次数保守重置。同一天多个来源成功不成为多次间隔确认。受影响的旧题目退役，旧手机提交返回 409 并重新取题。

停止服务后，可以先预览凭据，再撤销尚无新增作答的分组：

```sh
cd backend
.venv/bin/python scripts/rollback_learning_merge.py --db /path/to/vocabulary.db
.venv/bin/python scripts/rollback_learning_merge.py --db /path/to/vocabulary.db --receipt 1 --apply
```

撤销前再次备份，恢复来源状态并阻止启动时自动重新合并。若归并后已有新作答，脚本拒绝覆盖，保留凭据及新事件供后续按原始义项规划撤销；不能用旧整库覆盖新学习。课程范围保持已审核的四个来源用法。

验证须显式设置临时 `DATA_DIR`，使用 `tests/test_learning_units.py` 覆盖范围、共享曲线、跨账号隔离、轮换与固定、评分归属、旧题退役、历史保留及撤销；生产发布前在生产数据库的 SQLite backup 副本上预演。

## 部署提醒

学习提醒默认在用户时区的每天 20:00 发送，仅当当天 `review_history` 没有任何答题记录时发送；答错也算学习。无需设置开关或提醒时间。每个已授权设备每天最多成功发送一次，20:00–20:09 内允许短暂故障恢复，之后不补发；推送有效期 10 分钟。HTTP 发送失败最多尝试三次，失效订阅自动移除，退出账号解除当前会话的订阅。

iOS 16.4+ 需从主屏幕 Web App 使用，并通过受信任的 HTTPS 访问。首次点击现有“开始学习”按钮时请求 iOS 系统通知权限，不增加授权按钮或设置项。点击主屏幕图标本身不能代替网页内的用户动作。拒绝授权后不会重复请求，已授权设备会在登录或返回前台时同步订阅。通知点击进入学习页。Service Worker 同时保存完整的应用静态资源，断网重开可只读浏览当前账号已缓存的词义；个人 API 和答题响应不进入 Service Worker 缓存。缓存细节见 [deploy/CACHE.md](deploy/CACHE.md)。

部署说明见 [deploy/README.md](deploy/README.md)。

公网部署时至少要做到：走 HTTPS 并设置 `COOKIE_SECURE=true`；设置 `REGISTRATION_CODE` 以免被任意注册；把 `DATA_DIR` 指向持久化目录。

数据库 v13 在升级前通过 SQLite backup API 备份，新增 `user_sense_examples` 保存每个账号、每个义项的固定例句。升级时比较已发题记录和答题历史，固定最早仍有效的例句；原学习历史、复习计划与账号数据保留。
