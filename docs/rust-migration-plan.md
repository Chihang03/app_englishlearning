# Rust 后端与内容处理改造方案

日期：2026-10-22。状态：设计方案，尚未实施 Rust 迁移。

## 1. 改造目标与范围

采用 Rust 重写线上后端及高开销的内容处理路径，保留 React / TypeScript 前端、现有 SQLite 数据和离线学习协议。最终线上 API、提醒任务、内容校验与发布工具均可脱离 Python 运行；词典来源提取、人工审核交换、翻译等开发工具可以继续使用 Python。

这次改造的四个目标分别对应可验收的实现：

| 目标 | 实现位置 | 验收方式 |
| --- | --- | --- |
| 降低大型内容处理的对象与复制开销 | Rust 逐条解析、带缓冲写入、磁盘暂存、稳定 ID 映射 | 当前真实内容包与放大数据的峰值 RSS、内容指纹和导入结果对照 |
| 限制每条请求的资源使用 | 请求体限制、在途请求配额、有界队列、数据库执行期限、文件流背压 | 超大输入、排队、慢客户端、断连和超时后的资源恢复 |
| 加快批处理 | 独立 CLI、预编译语句、受限流水线、增量内容发布 | 与已优化的 Python 版本在相同机器、内容及数据库上比较 |
| 让部署产物独立 | 预编译 Linux 二进制、前端静态文件、版本清单和内容包 | 无 Python / Node 运行时的干净环境启动与功能验证 |

本次语言迁移同时保持产品行为：iOS Web App 的页面、语音、Service Worker、IndexedDB、账号与学习记录延续现状。首次词库下载量和浏览器解析内存，仍是前端协议的独立问题；Rust 后端迁移不能自动消除这些开销。目录分片或差量同步列为后续优化，首版保持现有 API。

## 2. 现状与基线

本轮静态检查：`backend/app` 有 34 个 Python 文件、约 6,700 行代码，共 45 个路由定义；数据库版本为 v22。后端依赖包括 FastAPI、Uvicorn、`webauthn==3.0.1`、`fsrs==6.3.0`、`pywebpush==2.5.0`。

当前关键调用链：

- 服务启动：`main.lifespan` → `database.init_database` → 迁移与指纹检查 → 必要时依次同步词库、译文、来源、词形、学习单元、频率和语义核查包。
- 目录：`/api/study/meaning-catalog` → `catalog_snapshot` → 按数据库身份、账号和内容修订号复用磁盘快照 → 304 或文件响应。
- 出题：`/api/next` → `next_sense_card` → `BEGIN IMMEDIATE` → 恢复或创建学习轮次；虽然是 GET，但具有写入行为。
- 答题：`/api/review` → `record_sense_review` → 同一事务校验学习轮次、写历史、更新复习状态与 FSRS 卡片、完成轮次。
- 提醒：systemd 每分钟启动独立任务 → 用户本地 20:00 起十分钟窗口 → 数据库领取发送资格 → 网络发送 → 更新发送结果。

此前优化验证中，单独读取词义核查包的峰值 RSS 从 566.5 MiB 降到 18.6 MiB，词库规范化哈希从 527.9 MiB 降到 76.0 MiB，详见 [小内存运行说明](../deploy/MEMORY.md)。这些是 macOS 独立 Python 进程结果。此前服务器验证时主进程 RSS 约 65 MiB、服务重启到健康约 2 秒；它们是历史基线，不代表本次重新采样，也不是持续运行或并发峰值。

因此，Rust 对照对象必须是当前已优化的 Python 版本，不能仅与事故前实现比较。开始实施时保存准确提交、完整依赖、数据包 SHA-256、服务器架构、SQLite 版本和测试场景。

## 3. 总体架构与技术选择

```text
iOS Safari / 主屏幕 Web App
            |
       Nginx：TLS、静态文件、连接与发送期限
            |
       Rust API：Axum / Tokio
        |         |          |
   学习与账号   内容目录    有界任务与运行指标
        |         |
    SQLite 专属工作线程    不可变目录文件
        |
 /var/lib/englishlearning/vocabulary.db

开发机 / 构建环境：来源提取与审核工具（允许 Python）
            |
   englishctl content build / validate
            |
     不可变内容包 + manifest
            |
   独立发布步骤：englishctl content apply

systemd timer → englishctl reminders run → 同一持久化数据库
```

技术选择：

| 部分 | 选择与理由 |
| --- | --- |
| HTTP | Axum + Tokio + Tower / tower-http，复用请求限制、跟踪和文件服务能力 |
| 数据库 | `rusqlite` + 固定专属线程 + 有界消息通道，精确控制事务、backup、进度回调和中断 |
| JSON | Serde / serde_json；逐条 Visitor 解析，typed struct，缓冲输出 |
| 时间 | chrono / chrono-tz 或等价成熟组件；应用层统一兼容现有 UTC 字符串和本地日期规则 |
| 密码与会话 | 现有 scrypt 格式适配器、SHA-256 token 哈希、加密安全随机数；使用成熟密码库 |
| 通行密钥 | webauthn-rs 生态；先验证旧 COSE 公钥和 user_handle 的导入能力，再确定具体适配接口 |
| FSRS | 首版实现固定 Python 6.3.0 行为的 Rust 兼容层；原生 fsrs-rs 经对照验证后再接入 |
| 推送 | web-push 的消息构建能力 + 可配置超时、重定向策略的复用 HTTP 客户端；VAPID 格式先做兼容试验 |
| CLI / 日志 | clap、tracing；结构化运行记录，不写入密码、Cookie、推送密钥或完整答题正文 |
| 发布 | Cargo workspace、提交 Cargo.lock、固定工具链、Linux release 构建、清单校验 |

选择 rusqlite 而非上次评估中的 SQLx，是对本方案资源控制目标的细化：项目现有 SQL 和 SQLite 事务较重，又需要 backup、执行进度和中断，固定线程更直接。同步 SQLite 调用只能在数据库专属线程执行，不能阻塞 Tokio 执行线程。首版只实现一套数据库适配器。

建议 workspace 保持五个 crate，避免拆成微服务：

```text
rust/
  Cargo.toml
  Cargo.lock
  rust-toolchain.toml
  crates/
    core/       # 领域类型、纯学习规则、FSRS 兼容与时间/文本规范化
    storage/    # SQL、事务用例、数据库工作线程、迁移与备份
    content/    # 内容流、暂存包、校验、指纹、目录构建
    server/     # Axum 路由、认证适配、资源配额、HTTP DTO
    cli/        # englishctl：内容、数据库、管理员和提醒命令
  tests/
    contracts/
    fixtures/
    compatibility/
    resource/
  benches/
deploy/rust/    # unit、发布与回退脚本、配置示例
```

依赖方向：storage → core；content → storage / core；server、cli 组合前三者。客户端不能通过直接引用存储实现绕过账号范围校验。表结构迁移只有一个权威入口。

## 4. 现有模块的迁移映射

| 当前模块 | Rust 位置 | 主要迁移要求 |
| --- | --- | --- |
| main.py、http_cache.py | server/routes、server/cache_policy | 路由、DTO、状态码、错误体、缓存和静态文件行为 |
| auth.py、security.py | server/auth、core/security、storage/accounts | scrypt、会话、角色、邀请注册、时区与 Cookie |
| passkeys.py | server/passkeys、storage/passkeys | 旧凭据读取、挑战一次消费、计数器、备份标记、Origin |
| push.py、learning_reminders.py | server/push、cli/reminders、storage/push | 订阅归属、签名密钥、发送领取与结果、时间窗口 |
| sense_learning.py、srs.py、adaptive_memory.py | core/learning、storage/study | 出题和答题事务、复习队列、FSRS 与个体校准 |
| senses.py、learning_units.py、learning_filters.py、muted_words.py | core/lexicon、storage/learning_units | 义项/单元锚点、共享与私有归属、静音与学习筛选 |
| study_tools.py、learning_calendar.py | storage/study_tools、server/calendar | 提示与曝光写入、本地日边界、统计返回字段 |
| morphology.py、word_forms.py、ing_usage.py | core/morphology、storage/lexicon | 目标词形验证、词性分组、反查顺序和现有回退规则 |
| database.py、migrations.py | storage/db、storage/migrations | v22、外键、索引、触发器、备份和内容同步拆分 |
| meaning_catalog.py | content/catalog、server/catalog | 有界构建、账号隔离、版本固定、ETag 和流式文件响应 |
| json_stream.py、vocabulary_json.py | content/stream、content/fingerprint | 顶层数组逐项解析、完整校验、旧指纹规范兼容 |
| chinese_glosses.py、chinese_sentences.py、source_completions.py、word_frequencies.py、content_overrides.py | content/importers | 完整性检查、来源绑定、受控覆盖和重复执行 |
| semantic_review.py、semantic_staging.py | content/semantic | 严格校验、输入/输出指纹、核查回执、撤销语义 |
| admin.py、report_review.py | server/admin、content/report_exchange | 角色权限、内容修订、乐观校验、dry-run 与审计 |
| backend/scripts | cli 或保留开发工具 | 生产必须的 apply / backup / admin / reminders 原生化；macOS 来源提取等工具后置 |

## 5. 大型内容的流式处理和批处理

### 5.1 从文件到内容包

统一管线：`原始文件 → BufReader → 顶层 MapAccess / 数组 SeqAccess → 单条强类型记录 → 校验 → staging SQLite → 完整包校验 → manifest`。

- 使用自定义 Serde Visitor / DeserializeSeed 逐项消费 `words`、核查记录等数组。`from_reader::<WholeDocument>` 仍会构造整份对象；`StreamDeserializer` 也不能直接当成顶层对象内部数组的逐项迭代器。
- 输入缓冲初值 64 KiB，输出缓冲 64 KiB；工作批次上限先设 128 条或累计 1 MiB，先到者触发，单条记录另设上限。
- 从文件流解析一般不能借用 `&str` 实现零复制；使用拥有所有权的单条结构、move 和缓冲复用，收益目标是减少整份对象与跨阶段复制，不宣称完全零复制。
- 大型引用关系、去重和排序进入 staging SQLite 的索引，不建立覆盖全词库的大 HashMap。小枚举和词性代码可以驻留。
- 格式层先限制单条字符串长度、集合长度、嵌套深度和总字节；超长 JSON token 要在构造完整字符串之前由计数读取/词法限制层拒绝。输入上限须覆盖当前合法包的最大值再定稿。
- 对顶层对象的元信息、所有数组、尾部 EOF 和重复关键字段做完整校验。包尾损坏或引用缺失时整包拒绝，不先应用前半部分。
- 内容包中只包含内容实体、稳定业务键和校验元信息，不打包生产账号、会话、学习历史或推送订阅。

读取与逐条序列化能力参考 [serde_json Deserializer](https://docs.rs/serde_json/latest/serde_json/struct.Deserializer.html)、[Serde SeqAccess](https://docs.rs/serde/latest/serde/de/trait.SeqAccess.html)。

### 5.2 指纹与旧版本兼容

区分三类值：原始文件 SHA-256、旧 Python 内容规范化指纹、新内容包格式版本。不得混用。

旧指纹遵循现有 Python 的对象/数组顺序、UTF-8、分隔符、转义和数值格式；Rust 默认 Map 顺序或浮点序列化可能不同。为当前全部内容文件建立精确对照，补充 Unicode、转义、浮点、负零、BOM 和旧格式组合用例。没有通过前，不写入“已同步”标记。

如果无法用通用 Serde 输出复现旧规范，就实现有版本的专用规范化适配器，并保留旧指纹识别。新格式只能显式升级，不能让语言切换触发词库误判和全量重写。

### 5.3 应用内容包

建议命令接口，具体参数实现时固定：

```text
englishctl content build --source <source-dir> --output <bundle-dir>
englishctl content validate --bundle <bundle-dir>
englishctl content apply --bundle <bundle-dir> --data-dir <data-dir> --dry-run
englishctl content apply --bundle <bundle-dir> --data-dir <data-dir>
```

应用流程：完整验证 staging 包 → 读取内容期望版本 → 获取发布锁 → 备份 → 在生产数据库中重新核对期望版本 → 单个事务按稳定键 UPSERT / 归档 → 校验引用与学习记录保护条件 → 写内容版本与回执 → 提交 → 刷新目录。

事务内分批执行语句，但默认不分批提交；这样语法/业务失败可以整体回滚。当前量级先采用短暂停止写流量的内容发布窗口。以后确实需要在线分段提交时，应新增完整的导入状态机与可见版本隔离，不能直接把单事务改为每几百条提交。

禁止用新建的内容数据库覆盖 `vocabulary.db`；禁止 `INSERT OR REPLACE` 造成主键/关联关系意外变化。保持现有 word / sense / lexical unit / learning unit ID 和历史关联。

增量比较以每条稳定内容键及哈希为依据，只有字段变化才写入，避免无变化的 UPDATE 触发目录修订。重新应用同一包应为空操作。作业失败没有成功回执，重跑从验证和期望版本核对开始。

### 5.4 批处理提速

优先级：消除重复全量读取 → 复用解析缓冲 → 预编译 SQL → 减少重复查询与无变化写入 → 再增加受限并行。

本地构建初始最多两个 CPU 工作任务；约 1 GiB 生产机初始一个。有界通道传递移动后的结构体，按照序号确定最终写入顺序。SQLite 只有一个写事务，并行解析不能变成多个竞争写入者。

生产 CLI 独立进程运行；Web 启动路径只读配置、模式版本和内容清单，绝不自动解包、迁移或重建整份词库。重任务使用低 CPU / IO 权重，发布时暂停提醒和在线写入。

## 6. 请求与进程资源预算

以下数值是首轮压测起点，均为建议值，不是已经测得的容量或保证。配额按路由和工作种类共享，避免每个端点各有一份从而累积失控。

| 资源 | 初始值 | 行为 |
| --- | --- | --- |
| 普通 JSON 请求体 | 64 KiB | 提取与全局 Body 层双重约束，超限 413 |
| Passkey 验证请求体 | 128 KiB | 与实际合法凭据样本验证后定稿 |
| 管理审核导入请求体 | 8 MiB | 独立权限/配额，最多保留现有 1,000 条；校验合法最大批次，过大内容走 CLI |
| 普通在途 API | 16 | admission 在解码前生效；健康检查单独保留极小配额 |
| 管理导入/校验等大请求 | 全局 1，不排队 | 在读取和解析请求体之前领取；避免多份 8 MiB JSON 展开后叠加 |
| 目录文件传输 | 全局 4，每账号最多 2 | permit 持有到响应体完成或丢弃，背压读取；超额拒绝 |
| 目录构建 | 全局 1 | 相同数据库/账号/修订只构建一次，等待者不占额外构建线程 |
| scrypt 等高开销认证操作 | 全局 1 | 所有登录、注册、改密码和管理密码校验共享；等待最多 4 |
| 在线 DB | 1 个写线程、2 个只读线程 | 每线程独占连接，固定线程数 |
| DB 等待队列 | 写 16、读合计 16 | 总在途约束仍有效，排队只保存有界请求数据 |
| 目录 DB | 构建时独立只读连接 1 | 构建结束关闭，避免长目录占满普通查询线程 |
| SQLite 页缓存 | 每连接初始 2 MiB | 不视为 SQLite 全部内存硬上限；排序、临时表和语句缓存另计 |
| 文件发送缓冲 | 每流 64 KiB | 不读取完整 35 MB 左右目录为 Vec / String |
| 目录磁盘缓存 | 初始总额 512 MiB | 先验证当前包及多账号占用；按版本/最近访问清理，可淘汰当前冷缓存并按需再生 |
| 普通 JSON 响应 | 目标不超过 2 MiB | 现有合法最大返回先审计；大列表若需分页，单独变更协议和前端 |
| 在线 CPU 工作 | 独立有界执行器 | 不使用 Tokio 默认阻塞线程池作为无限任务容器 |

普通请求排队最多 250 ms；认证排队最多 1 秒。过载返回统一 JSON 错误体和 503；明确的频率限制使用 429，两者提供适当 Retry-After。限制发生时不新增零散 UI 小字，沿用既有错误展示。

普通 HTTP 处理期限初始 10 秒，短于前端现有 15 秒；DB 普通语句预算初始 2 秒、busy 等待 1 秒，均在同一个剩余期限内。管理导入与冷目录构建采用独立预算，不能给所有端点统一 2 秒后宣称兼容。初次冷目录构建建议 30 秒预算；文件传输由代理负责空闲超时，应用同时限制每个流的总占用时间，初值 180 秒。

使用全局 RequestBodyLimitLayer 约束实际消费的字节，另外约束请求头和代理输入。仅配置 Axum 的 DefaultBodyLimit 不能覆盖直接读取 Body 的所有路径。[Axum 限制说明](https://docs.rs/axum/latest/axum/extract/struct.DefaultBodyLimit.html)、[tower-http 全局限制](https://docs.rs/tower-http/latest/tower_http/limit/struct.RequestBodyLimitLayer.html)

在合并路由前分别安装对应 Body 限制，并在 Nginx 同步相应 client_max_body_size；不能在所有路由外再套一个 64 KiB 上限导致 Passkey 或管理导入的专属上限失效。明确客户端压缩请求的支持策略；如支持解压，压缩前后均需限额。所有配额表项进入配置与启动日志，配置冲突时拒绝启动。

### 6.1 超时、取消与提交结果

- 为每个数据库操作绑定任务 ID、截止时间和取消标记；排队任务过期直接丢弃。专属线程在执行前、处理记录之间和提交前检查。
- 使用 rusqlite progress_handler 检查 SQL 执行期限；必要时 interrupt 当前任务。中断必须与当前任务 ID 绑定，不能让上一个 HTTP 请求的延迟取消误杀同连接的新查询。
- SQL 中断后回滚未提交事务、清理回调，确认连接回到 autocommit 状态再复用。
- HTTP 超时不能提前释放实际仍运行工作的 permit。响应与工作生命周期分开统计；后台工作真实结束后归还执行配额。
- scrypt 执行期间不能通过丢弃 Future 中止，必须把输入长度、并发数和等待队列先限制好。较长内容作业通过独立 CLI 进程边界限制。
- 提交成功但客户端已断连时，不能声称“超时所以没写入”；记录提交阶段，保留现有 attempt_id、唯一约束与重复提交处理。没有幂等设计的操作不自动重试。
- 若需要对某个任务强制执行 CPU / 内存硬限制，应移到独立进程。Rust 类型系统不能提供每条 HTTP 请求的硬内存隔离。

这些约束针对 Tokio 阻塞任务启动后不能直接 abort、默认可扩展大量阻塞线程的行为。[Tokio spawn_blocking 文档](https://docs.rs/tokio/latest/tokio/task/fn.spawn_blocking.html)

### 6.2 主机保护

API unit 初始建议 `MemoryHigh=192M`、`MemoryMax=256M`、`TasksMax=64`、`LimitNOFILE=1024`；内容作业另设 unit，初始 `MemoryHigh=256M`、`MemoryMax=320M`、低 CPU / IO 权重，重发布时停止在线写流量。最终数值依据实测 cgroup memory.current / peak 调整，不能只凭 RSS 定硬限制。

设置有限的服务重启频率，启动状态不匹配时快速报错，不反复全量导入。保留现有 Swap 作为峰值缓冲。进程缓存、文件页缓存、CLI 和 Nginx 均纳入整机预算；共享机器剩余内存不足时不并行跑基准、备份和构建。

systemd 的 MemoryHigh 是回收/节流阈值，MemoryMax 是最后保护线，可能触发该 cgroup 的 OOM；两者不能当作内存泄漏的修复。[systemd 资源控制文档](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)

## 7. 目录快照和 HTTP 缓存

延续数据库身份 + 账号 ID + 内容修订号的缓存键，保留内容表修订触发器。必须校验账号后再选文件，文件不能通过 Nginx 公共静态目录直接暴露。账号私有内容不会进入其他账号的缓存。

构建在独立只读事务中固定内容版本，按稳定顺序查询，一次拼装一个单词，`BufWriter` 写临时文件并同时计算 SHA-256。完成后同步数据、原子改名，并把正文与 ETag 元信息作为一个可验证的发布单元；中途失败不出现“元信息有效而正文不完整”。读者打开的是固定版本文件句柄，构建与发送分离。

重用缓存只读小元信息，ETag 匹配直接 304；200 的文件响应按背压发送，绝不先收集整份正文。慢客户端下载不持有数据库事务。正在读取的旧文件通过打开句柄或引用保护保证完整性。

同一修订的并发冷请求合并；过期缓存清理包含旧 .tmp 和孤立元信息，磁盘额度不足时先回收可重建文件。设定构建耗时上限，监测长读事务对 WAL 的影响；构建被取消时回滚快照并删除临时文件。

首版优先复用旧快照与 ETag。若 Rust 序列化字节变化，必须使用不同缓存格式命名并重新计算 ETag，允许一次 200 刷新；绝不能拿旧强 ETag 标识不同字节。前端当前会在 ETag 改变后清理相关缓存，应验证这一条实际路径。压缩内容的强/弱 ETag 和 Vary: Accept-Encoding 规则需要统一，避免同一强标签覆盖不同内容编码。

响应保留既有 Cache-Control、Vary、认证边界和错误行为。新响应大小限制要先审计已有列表，不能静默截断旧接口。

## 8. 数据库、迁移和发布状态

第一阶段 Rust 只接受现有 v22 数据库，保留全部表、ID、索引、约束、触发器和 UTC 时间表示。API 以验证模式启动，未知新版本立即拒绝，不尝试自行降级。

历史 v0–v21 升级在过渡期仍由现有 Python 一次性工具处理，随后再把迁移全部移到 Rust CLI；迁移退休前保留测试依据。最终提供 v22 新建数据库的权威基线，以及后续有编号的 SQL / Rust 迁移；不能把 `CREATE TABLE IF NOT EXISTS` 当成完整旧库升级。

新增迁移只由 `englishctl db migrate` 执行，API、提醒进程不会各自跑迁移。新增内容版本记录或 WebAuthn 适配字段时优先使用可回退的附加表/列，明确是否需要 v23；没有实际字段需求不提前升版。

发布操作沿用 `.startup.lock` 的跨进程排他约定，让过渡期 Python 提醒也能跳过发布窗口。正常 API 不长期持锁；进入维护窗口后先停止接入、排空请求、停止提醒，再进行备份和迁移。

备份必须使用 SQLite backup API 或在所有连接关闭后的可靠文件复制；不能在线只复制主 .db 忽略 WAL。完成 `quick_check`、`foreign_key_check` 和关键记录保护检查。SQLite WAL 仍只有一个同时写入者，连接池增加不能改变这一限制。[rusqlite 连接能力](https://docs.rs/rusqlite/latest/rusqlite/struct.Connection.html)、[SQLite WAL](https://www.sqlite.org/wal.html)

验收/压测只使用临时 DATA_DIR、合成数据和服务器内部隔离副本。真实生产数据库及账号材料留在服务器受限目录，不把用户数据下载到开发机。

## 9. 学习、FSRS 与身份兼容

### 9.1 学习协议

对现有 HTTP 请求建立可执行契约：字段名、类型、null 与缺省、数组顺序、状态码、错误 `detail`、Cookie、缓存头、公开/受保护接口、只读/写入性质。Pydantic 的长度按字符和字节限制不同，Rust 不能用 String.len() 直接替换所有 max_length。

保留 `/api/next` 的既有 GET 行为和 no-store；保留轮次恢复、提示/曝光记录、跨设备串行化、静音词筛选和账号权限。`/api/words`、`/api/words/import` 保持 410，不在迁移中恢复下线的写入功能。前端不会为配合后端改写而更改学习流程。

快照对照采用固定时钟和可控随机源：两份完全相同的隔离数据库分别跑 Python / Rust；比较响应和提交后的表。生产主库不能双执行 `/api/next`、答题、提示、曝光、登录挑战或内容导入。

文本规范化显式兼容 Python 的 strip、casefold/大小写、Unicode、撇号、连字符、词形和答案规则。日期计算按用户时区，覆盖 UTC 日界、夏令时 23 / 25 小时日及零点边界；时间字符串排序与现有数据库保持一致。

### 9.2 FSRS 首版固定语义

当前 `MODEL_VERSION` 是 `fsrs-6.3.0-retention80-v2`；保持目标保持率 0.8、禁用 fuzz、无 learning/relearning steps、最大间隔 365、现有 Good / Again 映射、辅助与练习分支、24 小时独立确认、180 天成熟判定和个体校准逻辑。

不能直接使用 `FSRS::default()` 替换。此次查到的 fsrs-rs 主分支同时含 FSRS-6 / 7，默认参数随实现演进，并且参数和状态使用 f32；现有 Python 使用双精度运算。crate 版本号也不能当作算法版本号。[fsrs-rs 当前实现](https://github.com/open-spaced-repetition/fsrs-rs/blob/main/src/inference.rs)

首版决策：在 `core/fsrs_compat` 中固定 Python 6.3.0 的算法、参数和 Card JSON 适配，以 f64 计算，并移植应用层校准规则。使用项目参考实现的许可并保留归属。Python 只作为离线对照工具，不作为线上 RPC 依赖。原生 fsrs-rs 只有在兼容验证通过后才能替换；算法升级单独立项、单独变更 model_version。

具体保护：

- 直接读取旧 `card_json` 的 card_id、state、step、stability、difficulty、due、last_review；不全量重算历史卡片。
- 复现 Python round 的 ties-to-even，不使用不同语义的默认 round；保持二分求间隔的迭代次数及上下界。
- 比较首次答题、同日重复、长间隔、答错、练习、使用提示、成熟边界和个人 multiplier 两端。
- 校准维持 150 样本、每 50 条检查、至少 20 单元/30 天、80/20 时间分割和现有采纳阈值；先同序遍历，避免并行浮点归约改变决策。
- 浮点内部状态设严格容差，初始参考绝对/相对 1e-9；但 interval、due 日期、mature、采纳决策和返回取整值要求一致。边界不一致不能仅靠放宽容差放行。

### 9.3 账号与 Passkey

保留 `scrypt$n$r$p$salt_hex$derived_hex`、当前 N=16384 / r=8 / p=1、随机盐、token SHA-256、`cvt_session`、30 天会话期、HttpOnly、SameSite=Lax、Secure 和 path。未知账号仍执行等价密码校验，已有密码无需重置。

优先进行旧 WebAuthn 凭据桥接试验：当前保存 credential_id、COSE public_key、sign_count、device_type、backed_up、transports，以及独立 user_handle。webauthn-rs 的完整持久化类型未必能仅由这些字段无损构造，不能假定任意库都可直接读取。

实现时选择其受支持的凭据导入或底层适配能力，必要的附加状态写新表并保留旧字段。复用原 RP ID、受信任 Origin 与 user_handle，继续强制用户验证、挑战一次消费、计数器/备份资格检查与会话创建事务。挑战状态始终由服务端保存，新版本上线时允许尚未完成的挑战过期重试，已经登记的密钥必须继续可用。

完成真实旧凭据登录、同步 Passkey 零计数器、多设备、删除与并发登录验证后才迁移这组路由。桥接无法成立时，该阶段保持 Python 处理整组认证流程，继续解决兼容问题；不要求所有用户重新绑定，也不宣称独立部署已完成。[webauthn-rs 文档](https://docs.rs/webauthn-rs/latest/webauthn_rs/)

## 10. 推送、管理和后台任务

Rust `englishctl reminders run` 替换 Python oneshot，继续由 systemd timer 每分钟调用，数据库领取资格和幂等身份保持 `(user_id, local_day, endpoint_hash)`。保留失败重试次数/间隔、超时后不贸然重发、404/410 删除和本地时间窗口。

网络发送在领取事务提交后执行，不能持有 SQLite 写锁等待推送服务。网络客户端复用、最多两个并发发送、十秒超时、禁止重定向，endpoint 校验与现有实现一致。原 VAPID 密钥与订阅原样保留；密钥材料格式和 Apple 推送响应在隔离试验中验证。

dry-run 仅报告候选，不发送、不写发送记录。发送结果不能承诺网络意义上的 exactly-once。按现有产品行为处理不确定结果。

管理审核保持 `extra=forbid` 等严格字段校验、manifest 批次指纹、原内容期望值、dry-run、回执和撤销规则。大规模语义审核、译文与来源处理优先在 CLI 执行，小批量既有管理 API 保持兼容并使用独立资源配额。[Rust web-push 能力](https://docs.rs/web-push/latest/web_push/)

## 11. 构建、部署与回退

### 11.1 产物

```text
release/
  bin/english-server
  bin/englishctl
  frontend/dist/
  content/manifest.json
  content/bundle.sqlite
  migrations/
  release-manifest.json
```

API 默认由 Nginx 提供同源代理，静态资源也由 Nginx 提供。运行期不需要 Node / npm。Rust 不把数十 MiB 内容或前端编译进二进制，避免每次内容更新都重编译。

Nginx 保持 HTML、版本信息、Service Worker 与带哈希资源的现有不同缓存策略。SPA 的前端路由回退只用于页面导航，未知 `/api/*` 仍返回 API 错误，不能退回 index.html。现有 IPv4、IPv6、8443 HTTPS 和 Passkey 域名均纳入发布验证；代理头只信任本机 Nginx，不能因代理调整改变受信任 Origin。

先实测目标服务器 `uname -m`、发行版和 libc；在匹配的 Linux 构建环境或 CI 构建，不能把 macOS 二进制复制到 Ubuntu。默认使用匹配发行版的 glibc 构建；是否改 musl 静态链接，以 WebAuthn / TLS / DNS 依赖验证决定，不提前承诺完全静态。

rusqlite 可 bundled SQLite；OpenSSL 等原生依赖根据实际解析结果固定。交付记录编译器、Cargo.lock、全部文件哈希及动态依赖清单。时区数据使用内嵌或明确随包提供的方式，推送 HTTPS 的 CA 根证书也要在干净运行环境验证。

release 构建不在小内存生产机运行。CI 执行格式、clippy、测试和构建；LTO / codegen-units 等配置按构建成本实测选择。保留可定位崩溃的符号与构建 ID，不盲目为小几 MiB 体积关闭全部诊断。

### 11.2 过渡方式

初期新服务仅监听服务器内部端口，使用隔离数据库验证。只迁移经过权限和副作用审计的整组路由，不按 GET / POST 粗分。共享生产库的短期路由切换只允许一致的学习/账号协议：整组学习写操作仍由一个实现负责，单一迁移入口、单一提醒发送器，跨进程写入仍依赖 SQLite 事务而非进程内互斥。

最终新旧服务不对同一主库同时提供写流量。灰度优先让测试账号访问绑定隔离库的新实例，完成后维护窗口整体切换；约 1 GiB 主机不要同时跑两套满负载进程。

### 11.3 正式切换

1. 新版本与哈希清单先上传独立 release 目录，做无需触碰主库的产物检查；记录旧 symlink、unit、配置与内容版本。
2. 入口进入短维护状态，停止 timer 并等待正在运行的提醒结束；停止旧 API 接入并排空/关闭写请求。
3. SQLite backup、完整性与关键学习表保护检查；按发布清单运行唯一迁移/内容应用命令。
4. 原子切换 release symlink / unit 到 Rust 产物，启动服务，在内部端口验证 readiness、会话、目录与版本。
5. 恢复代理流量；验证公开 HTTPS 和既有登录后恢复提醒 timer，观察资源、锁等待和错误。

`/api/health` 保持兼容，新增仅内网使用的 readiness，检查模式和内容清单匹配。新版本有 Python fallback 路由时，发布清单明确列出，不把过渡态算作独立部署完成。

### 11.4 回退

停止新请求与提醒，排空 Rust 写入，切回旧 release / unit 后重新启动。默认回退代码和内容资源引用，保留生产库的新学习记录；只有明确的数据损坏恢复程序才使用备份，不把“回退代码”等同于恢复旧数据库。

迁移要求至少保留一个回退版本的数据读写兼容性；若新增结构不能兼容旧代码，该版本必须先提供前向/反向数据转换并演练。特别核对旧 `startup_sync` 指纹：Rust 内容变化可能让旧 Python 再次执行旧包同步。回退前必须比较内容清单，使用匹配内容资源并受控验证，不能让旧启动代码自动覆盖新内容。

## 12. 实施阶段与完成门槛

采用阶段节点，不预设日历工期。Rust 兼容层、旧 Passkey 凭据桥接和内容稳定键是工作量较高的部分。

| 阶段 | 具体工作与交付物 | 完成门槛 |
| --- | --- | --- |
| R0 基线与契约 | 固定参考提交；生成 45 路由契约；测试夹具、时钟和表快照比较器；记录性能基线 | 账号隔离、GET 写行为和全部生产命令均有清单；既有失败单列 |
| R1 工程与兼容试验 | workspace、Linux 构建、配置/错误层、DB 工作线程原型；FSRS f64 对照和旧凭据桥接试验 | 核心依赖和部署环境可行；验证关键兼容路线后扩展开发 |
| R2 内容管线 | 流式解析、旧指纹、staging 包、validate/apply/dry-run、目录快照 | 当前内容包一致；损坏包零部分写入；重复导入空操作；内存受控 |
| R3 基础 API | 账号、Passkey、词典、设置、静音、词表、版本、缓存策略与资源准入 | 已有账号/密钥可用；错误契约通过；整组路由可受控切换 |
| R4 学习闭环 | 出题/答题/提示/曝光/学习单元/词形/FSRS/统计/日历 | 隔离库重放响应与状态一致；并发、断连、重复提交规则通过 |
| R5 管理与任务 | 管理审核、内容修正、原生提醒和生产 CLI、迁移历史收口 | 权限与回执兼容；只运行一个提醒发送器；推送协议与订阅行为验证 |
| R6 压测与切换 | 资源参数定稿、Linux 产物、服务器隔离验证、浏览器验收、回退演练 | 功能与资源验收通过，随后正式切换与观察 |
| R7 收口 | 删除生产 Python 依赖、过渡代理与重复实现；保留必要开发工具和对照测试 | 干净运行环境只用发布产物可完成所有线上功能 |

R2 的解析与 R4 的规则开发可以在接口约定后并行推进，但正式上线依赖对应阶段完整验收。FSRS / Passkey 关键试验在 R1 提前做，避免接口全部写完后才发现无法兼容。

R7 结束前，旧后端和参考测试保留在仓库可追溯版本中；不会在 Rust 初步启动时就删除 Python 实现。

## 13. 验证与验收

### 13.1 正确性

- 把现有 Python 测试按模块映射到 Rust 验收清单，覆盖 API / SQL / 纯规则三个层次。此前 135 项是一次内存优化的定向回归，不是全项目通过数。
- 既有两个词形失败独立记录和重现；在其覆盖范围迁移前，明确预期并修复或制定规范，不能把旧失败当作新后端可永久忽略的功能。
- 相同隔离数据库、时钟、请求序列，比较全部关键表的稳定列、关系和响应；随机 ID 用可控随机源或一致映射，不忽略业务差异。
- 账号/角色/私有词典/静音/设置/Passkey/推送订阅、内容修正回执，逐项检查隔离与权限。
- 旧会话、旧密码、旧 Card JSON 和旧凭据可继续使用；下线接口维持 410。
- 内容包尾损坏、重复字段、超长 token、引用缺失、导入中断、重复导入和版本冲突，均有明确失败结果及数据库保护证据。
- 缓存验证相同账号并发冷构建、账号切换、内容更新中下载、304、压缩与清理。

### 13.2 资源与速度

用相同 Linux 机器、release 模式、相同 SQLite / PRAGMA 与相同合成或服务器内部隔离数据比较。热/冷文件缓存和磁盘占用分开记录；测端到端时保留落盘/事务持久化，不能通过削弱校验或 durability 换成绩。

每种场景至少五次独立运行，报告样本量、median / P95、峰值 RSS、cgroup memory peak、CPU 时间、WAL 与临时磁盘增长、DB busy/排队、错误及拒绝率。成功请求延迟不能掩盖被拒绝的比例。负载生成器放在另一台机器；生产数据测试仅在服务器受限隔离目录进行。

| 场景 | 建议验收目标，实施前固定基线 |
| --- | --- |
| API 空闲五分钟 | Rust RSS ≤ 50 MiB；这是目标，不是预测 |
| 常规请求混合，1 / 5 / 20 客户端 | 已接纳请求 API RSS 峰值目标 ≤ 128 MiB；记录并发 20 超过配额时的拒绝与恢复 |
| 1 / 5 客户端常规出题/答题 | 服务端 P95 目标 ≤ 200 ms，排除目录下载、密码散列和维护窗口；同时不得劣于参考版本超过约定误差 |
| 未修改内容的启动到 readiness | 目标 ≤ 1 秒；不包含内容导入或数据迁移 |
| 当前内容包 validate/build | RSS 目标 ≤ 64 MiB；相对已优化 Python 同等校验吞吐目标 ≥ 1.5 倍，至少不能退化 |
| 内容放大到 5 倍 / 10 倍 | 合法单条最大尺寸相同，RSS 不随总条目近似线性增长；磁盘暂存和耗时增长有记录 |
| 完整内容 apply | 单独测总耗时、DB 写锁时间和峰值；RSS 初始目标 ≤ 128 MiB，提交时间不能用解析提速掩盖 |
| 冷目录构建与四路慢下载 | API 进程峰值初始目标 ≤ 128 MiB；下载不重复构建、不累积完整正文，不长期持有读事务 |
| 304 热请求 | 不扫描词典、不重新序列化，P95 不劣于基线 |
| 24 小时混合负载 | 无 OOM / 非计划重启；预热后内存不随请求数持续上升；定时记录 RSS / cgroup / 队列与临时文件 |

64 MiB、128 MiB 等目标按进程工作集评估；cgroup 包含页缓存等，应另外报告并与 systemd 限额比较。无法达标时先说明原因和资源构成，不能直接扩大硬限制后宣称通过。资源安全门槛和功能一致性必须通过；速度提升不足则如实记录，再决定优化范围。

### 13.3 iOS 实际验证

按 2026-10-22 最新验收范围，使用浏览器检查网页登录、学词/答题、目录缓存更新、账号切换与管理流程；通行密钥签名、旧凭据与挑战安全、推送订阅和回执由协议及数据库测试验证。设备特有行为按后续反馈处理，不再设置独立 iPhone 真机验收节点。

上线观察同时覆盖 API unit、reminders timer、Nginx、内核 OOM 和 SQLite busy。需要把新旧代码版本、内容版本、数据库版本、产物哈希与观察时间一起保存。

## 14. 主要风险与对应决策

| 风险 | 本方案的处理 |
| --- | --- |
| 更换 FSRS 实现导致复习日期漂移 | 先固定 f64 兼容层与旧模型，再单独评估原生库/算法升级 |
| WebAuthn 库无法无损导入旧凭据 | R1 提前验证，保留凭据字段；兼容失败不切换认证组 |
| Rust 仍整包解析、队列无限增长 | Visitor 逐项解析、staging 索引、全局准入和有界通道 |
| HTTP 超时后 SQL/散列仍消耗资源 | 工作级 deadline、中断/取消标记、真实完成才释放 permit |
| 批处理事务阻塞学习写入 | 内容构建离线化、apply 维护窗口、阶段耗时单独验收 |
| 回退旧服务覆盖新内容或学习记录 | 向后兼容结构、匹配内容清单、回退代码保留当前库 |
| 原生依赖使“单文件部署”不成立 | 干净 Linux 环境验证，随产物列明库/CA/时区；不承诺无条件静态链接 |
| 后端迁移被误认为 iPhone 内存优化 | 前端协议保持，另测目录解析和首载；后续分片单独推进 |

## 15. 本方案的最终交付定义

完成全部阶段后，交付：Rust 源码和锁定依赖、全量 API 兼容报告、学习与 FSRS 对照报告、内容指纹及导入验证、资源/性能对照、浏览器验收记录、生产 CLI、Linux release 产物、自动发布与回退脚本、部署清单及运行手册。

最终主机只依赖发布目录中的两个 Rust 可执行文件、前端文件、内容资源与明确的系统依赖，即可运行现有全部线上功能。开发侧保留 Python 来源工具不影响这个目标。

正式实施时遵循仓库的 main 提交、推送和部署约定；未完成阶段只产生内部验证产物，只有通过对应上线门槛的阶段才进入生产。本文档本身不修改运行代码或生产配置。

## 附录 A：当前 HTTP 路由清单

以下由当前 Python AST 提取，用作 R0 契约覆盖起点；仍需补齐每项的身份要求、请求/响应模式、副作用、缓存规则及异常路径。路由动词不能代替副作用审计。

| 方法 | 路径 | 来源 | 处理函数 |
| --- | --- | --- | --- |
| GET | `/api/admin/overview` | [admin.py](../backend/app/admin.py) | `overview` |
| GET | `/api/admin/users` | [admin.py](../backend/app/admin.py) | `users` |
| GET | `/api/admin/users/{user_id}` | [admin.py](../backend/app/admin.py) | `user_detail` |
| GET | `/api/admin/reports` | [admin.py](../backend/app/admin.py) | `reports` |
| GET | `/api/admin/reports/{report_id}` | [admin.py](../backend/app/admin.py) | `report_detail` |
| PATCH | `/api/admin/reports/{report_id}` | [admin.py](../backend/app/admin.py) | `update_report` |
| PATCH | `/api/admin/reports/{report_id}/content` | [admin.py](../backend/app/admin.py) | `correct_report_content` |
| POST | `/api/auth/register` | [auth.py](../backend/app/auth.py) | `register` |
| POST | `/api/auth/login` | [auth.py](../backend/app/auth.py) | `login` |
| POST | `/api/auth/logout` | [auth.py](../backend/app/auth.py) | `logout` |
| GET | `/api/auth/me` | [auth.py](../backend/app/auth.py) | `me` |
| PATCH | `/api/auth/me` | [auth.py](../backend/app/auth.py) | `update_profile` |
| PATCH | `/api/auth/password` | [auth.py](../backend/app/auth.py) | `change_password` |
| GET | `/api/learning-calendar` | [learning_calendar.py](../backend/app/learning_calendar.py) | `learning_calendar` |
| GET | `/api/health` | [main.py](../backend/app/main.py) | `health` |
| GET | `/api/stats` | [main.py](../backend/app/main.py) | `stats` |
| GET | `/api/next` | [main.py](../backend/app/main.py) | `next_card` |
| POST | `/api/review` | [main.py](../backend/app/main.py) | `review` |
| POST | `/api/study/hint` | [main.py](../backend/app/main.py) | `study_hint` |
| POST | `/api/study/related-exposure` | [main.py](../backend/app/main.py) | `related_exposure` |
| POST | `/api/study/meanings` | [main.py](../backend/app/main.py) | `study_meanings` |
| GET | `/api/study/meaning-catalog` | [main.py](../backend/app/main.py) | `study_meaning_catalog` |
| POST | `/api/content-reports` | [main.py](../backend/app/main.py) | `content_report` |
| GET | `/api/settings` | [main.py](../backend/app/main.py) | `settings` |
| GET | `/api/muted-words` | [main.py](../backend/app/main.py) | `muted_words` |
| POST | `/api/words/{word_id}/mute` | [main.py](../backend/app/main.py) | `mute` |
| DELETE | `/api/muted-words/{word}` | [main.py](../backend/app/main.py) | `restore` |
| PATCH | `/api/settings` | [main.py](../backend/app/main.py) | `patch_settings` |
| GET | `/api/word-lists` | [main.py](../backend/app/main.py) | `word_lists` |
| GET | `/api/dictionary/{word}` | [main.py](../backend/app/main.py) | `dictionary_lookup` |
| POST | `/api/words` | [main.py](../backend/app/main.py) | `retired_word_import` |
| POST | `/api/words/import` | [main.py](../backend/app/main.py) | `retired_word_import` |
| GET | `/api/version` | [main.py](../backend/app/main.py) | `frontend_version` |
| GET | `/api/auth/passkeys/config` | [passkeys.py](../backend/app/passkeys.py) | `configuration` |
| GET | `/api/auth/passkeys` | [passkeys.py](../backend/app/passkeys.py) | `list_passkeys` |
| POST | `/api/auth/passkeys/register/options` | [passkeys.py](../backend/app/passkeys.py) | `registration_options` |
| POST | `/api/auth/passkeys/register/verify` | [passkeys.py](../backend/app/passkeys.py) | `registration_verify` |
| POST | `/api/auth/passkeys/login/options` | [passkeys.py](../backend/app/passkeys.py) | `authentication_options` |
| POST | `/api/auth/passkeys/login/verify` | [passkeys.py](../backend/app/passkeys.py) | `authentication_verify` |
| DELETE | `/api/auth/passkeys/{passkey_id}` | [passkeys.py](../backend/app/passkeys.py) | `delete_passkey` |
| GET | `/api/push/config` | [push.py](../backend/app/push.py) | `config` |
| POST | `/api/push/subscription` | [push.py](../backend/app/push.py) | `subscribe` |
| GET | `/api/admin/report-review/export` | [report_review.py](../backend/app/report_review.py) | `export_reports` |
| POST | `/api/admin/report-review/validate` | [report_review.py](../backend/app/report_review.py) | `validate_reports` |
| POST | `/api/admin/report-review/import` | [report_review.py](../backend/app/report_review.py) | `import_reports` |


## 2026-10-22 实施记录

R2–R4 已以 `90ffbf4` 保存检查点。随后完成 R5 管理与任务及 R7 独立运行入口；临时 Python 目录桥接已移除，Python 仅保留为离线内容制作及兼容性参考。

本轮验证包括 44 项联合回归、180 个 FSRS 参考场景、真实数据的响应/目录对照、1/5/20 并发测试、同库 Rust→Python→Rust 回退，以及无 Python/Node/Cargo 的 chroot 内 29 个接口操作和原生备份。读取预算依据真实数据定稿为 5 秒，其他固定连接数、队列和内存边界保持。并发 20 会触发 503 准入保护，补充重试负载可完成全部请求；具体分布与限制见[性能报告](rust-migration-performance-report.md)。

生产已于 2026-10-22 12:34 切换至 `26.10.22.94` Rust release。浏览器学习闭环、设置、课程、账号安全列表和退出登录通过；临时测试账号已清理。域名/IPv4/IPv6 TLS 健康正常，原生提醒运行成功，观察期内 NRestarts=0，真实学习记录与密钥保留。首次版本检查触发自动回退后已修复 Nginx 重载交接检查，并成功重试。详见性能报告和聚合验收附件。
