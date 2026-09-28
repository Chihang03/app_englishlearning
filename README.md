# Context Vocabulary Trainer

语境单词训练器。系统用英文例句挖空让用户根据上下文和当前义项的解释回忆单词，并用 SQLite 持久化保存学习记录与义项级 SRS 状态。支持多用户，每人有各自独立的学习进度。

## 技术栈

- Frontend: React, TypeScript, TailwindCSS, Vite
- Backend: Python FastAPI
- Database: SQLite（WAL 模式）
- Auth: 会话 Cookie + scrypt 口令散列 + WebAuthn 通行密钥（Passkey）
- TTS: 浏览器 Web Speech API（`speechSynthesis`）

设置页底部显示程序版本号，例如 `26.9.27.27`。每次提交 main 前，在 `frontend` 运行 `npm run version:update` 并将生成的 `src/version.json` 一起提交；脚本按北京时间生成日期，末位为 main 主线（first-parent）的累计提交数加一，不随日期变化重置。重新运行脚本不会额外递增，重新构建同一提交也不会改变版本号。

设置中的“跳过基础 600 词”按 NGSL 通用高频源词表的前 600 个词头过滤，默认关闭，每个账号独立保存。开启后，这些词的所有义项均退出新词、到期复习和待巩固队列，跨词库和同词头的私有导入均生效。学习历史、SRS 和手动“不再学习”记录保留；关闭后恢复原有队列。它不修改例句，也不把跳过的词计为已掌握。

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
    adaptive_memory.py # FSRS、熟词先验与个人遗忘速度校准
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

### 动态记忆与熟词确认（数据库 v7）

复习以用户和义项为单位使用 `fsrs==6.3.0`。模型根据实际经过时间及独立作答结果更新记忆难度和稳定性，再计算约 90% 目标回忆概率对应的间隔；不再用固定阶梯决定后续日期。模型按 UTC 时间更新，复习日期仍按账号当地日历安排。原有到期日期在升级时保留，到下一次真实作答时才初始化记忆状态，旧间隔仅作为初始估计。

此前没有作答和例句预览记录的新义项，首次无提示答对时使用 30 天稳定性先验，首次间隔最多 30 天；第二次确认后，间隔由模型计算、最多 60 天。这是需要后续验证的熟词假设，不能将一次答对认定为永久掌握。默认参数下典型路径为首次通过 → 30 天后通过 → 再过 60 天通过。成熟要求至少三次相隔不少于 24 小时的独立确认、首次至今至少 90 天、稳定性至少 180 天，并且个人校准后的间隔达到 180 天，最近 180 天无错误。中途答错会退出熟词路径，保留纠正和至少 20 分钟后的独立巩固；普通词也由 FSRS 动态调度。

新模型判为 `Mature` 的义项每 180 天抽查，抽查到期与普通到期复习使用同一统计和选卡规则。失败后重新学习，单词全部当前有效义项成熟才计为成熟单词。升级前已成熟的记录保留原计划，不自动加入抽查或重新估计。

答题前朗读单词先通过 `POST /api/study/hint` 持久化提示记录，刷新或跨设备后仍生效。听提示后答对只记为辅助作答，不增加独立通过数，也不作为训练标签，至少 20 分钟后再次独立巩固。打开“其他意思与用法”前，通过 `POST /api/study/related-exposure` 记录相关义项的答案暴露，近期预览过的义项按辅助作答处理，曾预览过的义项不使用熟词先验。答案纠正不再次更新 FSRS。前端记录最多五分钟的活动作答耗时，页面隐藏、失焦或超过 30 秒无键盘/指针活动的时间不计入；这是辅助遥测，不用速度直接判定熟练。

每次真正延迟至少 24 小时、无提示的首答保存作答前的基础回忆概率、个人校准概率和模型版本。旧记录以及同轮纠正不补造预测。个人校准学习一个有界的遗忘速度系数：`P_personal = P_FSRS ** multiplier`，同时将其用于求解下次日期。它是 FSRS 上的单参数机器学习校准，并非深度神经网络，也没有修改 FSRS 的 21 个权重。

校准自动在答题后触发：至少 150 个有效样本、20 个义项、跨度 30 天，之后每新增至少 50 个样本重新评估，最近最多 5000 个样本按时间顺序以前 80% 拟合、后 20% 验证。只有验证集平均对数损失比默认模型和现有个人模型都改善至少 0.005 时才采用；否则保留原参数。如果原个人模型比默认模型明显差，则回退到默认参数。门槛是本应用的保守产品策略。拟合只用过去的在线预测和真实结果，不使用当前答案计算当前预测；改动只影响未来实际作答后的计划，不批量重排待复习词。

`GET /api/stats` 的 `memory_model` 返回样本量、校准状态和验证损失；设置页简要说明自动复习。数据不足时依然动态更新单词记忆状态，只使用默认人群参数。实际长期记忆收益需要后续真实延迟作答验证，时间模拟测试不能证明真人记忆效果。

学习页面不显示下次复习日期、确认次数文字、纠正成功、稍后复习或重新输入的提示。熟词确认用卡片左上方、“新词/名词”一行上面的三条短横线表示，已完成的次数显示为深色；只计算已经完成的确认，不把本次尚未作答的题目计入。答题后的确认进度配有无障碍描述。答错仅把填空处标红，修改答案和恢复题目时仍保持红色，答对后显示正确状态；输入框通过 `aria-invalid` 标记错误。网络错误与空题目时的词库选择建议保留；提示记录、校准和复习时间仍由后台照常处理。

运行后端验证：`cd backend && .venv/bin/python -m unittest discover -s tests -v`；前端验证：`cd frontend && npm run build`。v7 升级前自动用 SQLite backup API 备份，保留 WAL 中尚未检查点的数据。

每张卡只显示当前例句所属义项的解释和词性。学习页右上角的三点菜单提供“更多词义”和“报告错误”，左上角返回首页；首页通过底部导航进入设置。答题前后都可以打开“更多词义”，查看词库收录的解释与例句，当前义项会标记为“当前词义”；只有一个义项时明确提示。打开菜单或弹窗时暂停自动跳题，答对后可通过菜单的“下一题”继续。例句按账号和义项固定，后续复习与错题巩固沿用首次学习的例句。新义项优先选择答案与词头相同的原形例句；没有原形例句时使用该义项已有的例句。原句被词库归档后才选择并固定一个有效替代句。词性旁显示非原形提示，包括第三人称单数、过去式、过去分词、复数、比较级和最高级。-ing 答案结合固定例句，在明确结构下细化为动名词、现在分词或现在分词·进行时；不确定或多处挖空用法不同的句子保留 -ing 形式。

`POST /api/study/meanings` 使用当前 `attempt_id` 加载词义，在返回内容前持久化当前题目的答案暴露和其他义项的预览记录。答题前查看词义后，后续正确作答沿用辅助作答规则，不计为独立通过。报告错误只显示挖空例句，不揭示答案或改变学习进度。

错误报告可选择单词释义、例句、句子翻译、发音或其他问题，并填写最多 2000 字的说明。`POST /api/content-reports` 要求登录并校验题目归属，保存报告者、单词、义项、例句、内容快照和创建时间到数据库 v8 的 `content_reports` 表。同一用户对同一轮题目、同一类型的重试不会重复创建报告。界面只有收到服务器确认才显示提交成功；报告等待人工核对，不自动修改词库。管理员可在服务器数据库中查询 `SELECT id, category, details, content_snapshot, created_at FROM content_reports WHERE status='pending' ORDER BY created_at`。v8 升级前沿用 SQLite backup API 自动备份，原学习记录和计划保持不变。

SRS 按 `(user_id, sense_id)` 独立记录：答对“地址”不会改变 address 的“处理”义项进度。到期义项先复习，然后学习所选词表中的新词和未学义项；同一个义项跨四级/六级共用进度。旧词的其他意思显示为“新义项”，不重复统计为新单词。首页分别显示单词数和义项数，“已掌握单词”要求该词全部可学义项进入间隔复习。

今日成功要求在一轮新的作答中，未看答案就正确拼写例句所需词形。答错或空答会显示答案；在原题纠正正确只记录练习，不推进 SRS。纠正后至少间隔 20 分钟才会重新出题（后端按整秒向上取整，最长多 1 秒）。其他到期复习优先，其次是已到时间的待巩固义项，再其次是新词；错词尚未到时间时继续学习新词，界面不显示等待时间或倒计时。再次答错会重新纠正、排到队尾并重新计时。离开页面不影响服务器保存的到期时间。如果所选词库已经没有其他可学题目，界面建议选择其他词库，后台在待巩固词到时间时静默重新加载。

每张卡片带有后端签发的 `attempt_id`，答题时原样传回。刷新或重新登录会恢复本轮是否已看过答案；待巩固队列跨设备和跨天保存。同一义项在账号当地日期内最多独立通过一次，重复提交已完成轮次返回 409，前端会重新加载当前题目。首页“今日成功单词”按独立通过的单词去重，同时显示成功义项数；通过一个义项不表示该词所有意思都已掌握。独立作答正确率只统计每轮揭示答案前的作答，答题次数仍包含纠正。

20 分钟巩固间隔与后续 SRS 间隔一样，未到时间不计入待复习。首页和学习页的待巩固数量、待复习错词、到期义项以及卡片的剩余复习数，均只统计当前已到期且有可用例句的内容，与出题使用同一规则。到期时界面静默刷新统计，不显示等待时间；学习中、累计学过等进度仍保留尚未到期的词。

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

发音由浏览器的 Web Speech API 完成，不依赖服务端，因此部署到 Linux 服务器上同样可用。语速提供慢（90）、正常（120）、快（175）三档，默认正常；沿用旧的 175 基准换算浏览器语速倍率。选择立即生效，按账号缓存到浏览器 `localStorage` 并自动写入服务端设置，刷新或重新进入后恢复。保存失败时保留待同步选择，恢复网络或再次进入后重试；快速切换按顺序保存，加载中的旧值不会覆盖新的选择。英文语音仍按设备保存在浏览器中，可选声音取决于设备安装的英文语音。

## 账号与数据归属

每个账号有独立的 SRS 进度、复习历史和显示设置。`seed_words.json` 里的词属于**公共词库**（`owner_id` 为 NULL），所有人可见；通过 `POST /api/words` 或 `POST /api/words/import` 添加的词归添加者私有，其他账号看不到也复习不到。

## 时区

每个账号有自己的时区（注册时由浏览器自动填入，可用 `PATCH /api/auth/me` 修改）。复习时间戳统一以 UTC 存储，"今天"、每日统计、连续天数和复习到期日都在账号自己的时区里计算。这样服务器跑在 UTC 也不会让身处东八区的用户日界线错位。

## 数据库迁移

学习页右上角菜单中的「不再学习此单词」对当前账号下的整个单词生效，包括全部义项、跨词库的同词和以后导入的同词。操作后进入下一题，新词、复习、错题巩固和待学计数都会排除该词；历史作答和原有学习进度保留，不额外记作完成或掌握。可以通过操作后的「撤销」或设置中的二级页面「不再学习的单词 → 恢复学习」恢复原有安排。该页面按 A–Z 分组，列表使用屏幕可用高度，右侧首字母索引支持点击、拖动和键盘定位。schema v9 新增账号级消音表，升级前沿用 SQLite backup API 自动备份。

启动时会自动把数据库升级到当前 schema 版本（用 SQLite 的 `user_version` 跟踪），**升级前会自动在同目录留一份 `.bak-v<版本>-<时间戳>` 备份**。

从单用户版本升级时，已有的学习记录会全部归属到一个新建账号：账号名取自 `INITIAL_USERNAME`（默认 `admin`），密码取自 `INITIAL_PASSWORD`；未设置 `INITIAL_PASSWORD` 时会随机生成并在启动日志里打印一次，登录后可在界面右侧「账号 → 修改密码」自行修改。

## 部署提醒

公网部署时至少要做到：走 HTTPS 并设置 `COOKIE_SECURE=true`；设置 `REGISTRATION_CODE` 以免被任意注册；把 `DATA_DIR` 指向持久化目录。

数据库 v13 在升级前通过 SQLite backup API 备份，新增 `user_sense_examples` 保存每个账号、每个义项的固定例句。升级时比较已发题记录和答题历史，固定最早仍有效的例句；原学习历史、复习计划与账号数据保留。
