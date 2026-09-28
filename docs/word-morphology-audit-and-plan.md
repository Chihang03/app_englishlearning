# 词形归并 / 词汇派生：代码审计与实施方案

> 2026-09-28 第一版实施：新增 v14 身份表，保留原义项 SRS；词形包 v2 含 37 个审核单位、25 条屈折链接、15 条派生/词汇化关系及 saying 名词补充。反查返回多个身份，更多词义支持分类展示与关系详情，记录实际返回内容的曝光。未审核词形保留 UNKNOWN，既有训练记录不自动合并；后续阶段仍按本文保守迁移门槛实施。

审计日期：2026-09-28。代码基线：main，d781e75。面向 iOS Web App。

本轮只完成审计、验证和设计，没有修改应用代码、生产数据或部署。工作区原有的 AGENTS.md 修改保留。

**推荐结论：保留现有义项级复习，补充词汇单位、词形和带证据的关系。归并的对象是“同一词汇单位的同一义项”，不是两个拼写字符串，也不是整个词族。**

同一 say 动词义项的 say / says / said / saying 共用一份复习状态；say 的其他独立义项仍按现有逻辑分别学习。saying 的名词义项独立学习。absolute 与 absolutely 保持各自的义项、复习和成熟状态，只建立经过验证的派生关系。

## 审计依据与验证边界

实际阅读了导出器、字典解析器、静态词库、词形文件、SQLite 迁移与导入、义项及例句保存、出题与评分、FSRS 与成熟判定、静音与基础词过滤、管理员内容修正、前端学习卡片与“更多词义”弹窗及相应测试。

此外直接读取了本机已安装的词典 Body.data 原始记录：

- New Oxford American Dictionary：本机包版本 2.6，com.apple.dictionary.NOAD。
- Simplified Chinese - English：本机包版本 1.1，com.apple.dictionary.zh_CN-en.OCD。
- 从实际记录检查了词性区块、屈折、交叉引用、派生词区块、词源、多个同形词条及相关例句，没有依赖公开 API 文档猜测字段。
- [原始记录审计清单](/private/tmp/english-morphology-audit-20260928/raw-summary.json)保存包版本、Body.data 指纹和抽样条目标识；[临时运行时查询结果](/private/tmp/english-morphology-audit-20260928/runtime-evidence.json)保存实际查询函数返回的可用性及类型结构。清单中的原始 HTML 路径位于临时目录。
- 本机没有项目导出虚拟环境或 PyGlossary。本轮直接解压实际词典的记录进行只读检查；没有重新生成生产词库，也没有声称跑过完整 PyGlossary 重导出。
- 静态词库没有记录生成时的词典版本，因此本机当前源包与历史导出包版本相同这一点**无法确认**。

验证结果：

| 验证 | 结果 |
|---|---|
| 义项、记忆模型、词形、学习工具、基础词、静音、管理员相关现有测试 | 66 项通过 |
| 字典导出解析相关现有测试 | 10 项通过 |
| 验证当前目录全部义项及例句配对，并导入临时数据库 | 成功 |
| 临时数据库 schema / 外键检查 | v13 / 0 个违规 |
| 静态 catalog | 12,891 个词头、30,538 个义项、42,122 条例句 |
| 临时数据库导入后，包括 seed 的补充义项与例句 | 12,891 个 words、30,549 个 word_senses、42,134 个 sense_examples |

这些是**当前实现的基线验证**，不是下文新分类器或迁移方案已经实现的证据。

本机 backend/data/vocabulary.db 实际仍是 v2，12 个词、51 条历史、12 条旧 SRS。代码支持到 v13。本轮未检查服务器实时数据库，不能把本机样本数量或版本当作线上现状。实施前必须对服务器实际 DATA_DIR 做备份和只读盘点。

# 1. 当前实现

## 1.1 完整数据流

~~~mermaid
flowchart TD
  A[NGSL / 考试词表快照] --> B[源词头与词库归属]
  C[Mac 词典 HTML / 索引别名] --> D[按词性和义项解析]
  B --> E[跨词典保守对齐与例句筛选]
  D --> E
  E --> F[vocabulary_catalog.json v2]
  D --> G[word_forms.json v1]
  F --> H[SQLite words / word_senses / sense_examples]
  H --> I[用户与义项对应的固定例句]
  I --> J[持久化 study_attempts]
  J --> K[按 target_form 评分]
  K --> L[sense_srs_state / adaptive_memory / review_history]
  L --> M[下次出题与成熟度统计]
  G --> N[答案词形提示]
  H --> O[本地查询与更多词义]
~~~

实际入口：

1. [build_vocab_bundle.py:323](/Users/hang/Desktop/app_englishlearning/backend/scripts/build_vocab_bundle.py:323)从已本地化 source_word_lists.json，或显式传入的源快照生成词头及词库归属。
2. [read_mac_dictionary:216](/Users/hang/Desktop/app_englishlearning/backend/scripts/build_vocab_bundle.py:216)通过 PyGlossary AppleDictBin、html=True 读取 record.s_term 和 record.defi。s_term 是检索别名集合，包含屈折、派生等不同身份；它不是 lemma 映射。
3. [parse_record:186](/Users/hang/Desktop/app_englishlearning/backend/scripts/dictionary_senses.py:186)保留按原词典分组的词性、义项、释义、例句及 target_form。
4. [aligned_senses:270](/Users/hang/Desktop/app_englishlearning/backend/scripts/build_vocab_bundle.py:270)优先英汉义项；跨词典只在人工映射或词性一致且例句唯一匹配时合并。无可靠中文对应时可回退英文。
5. build 输出 catalog v2、word_forms v1、源词表和覆盖报告。
6. [init_database:62](/Users/hang/Desktop/app_englishlearning/backend/app/database.py:62)升级 SQLite，再导入 seed 和 catalog。部署服务器只读静态 JSON，不连接 Mac 词典。
7. [save_senses:39](/Users/hang/Desktop/app_englishlearning/backend/app/senses.py:39)按稳定键更新义项、按义项与句子更新例句；移除内容归档，不删除历史；管理员修正继续覆盖上游内容。
8. [next_sense_card:134](/Users/hang/Desktop/app_englishlearning/backend/app/sense_learning.py:134)优先恢复未完成轮次，再处理到期义项、到时的待巩固义项、新义项。
9. [example_for_sense:114](/Users/hang/Desktop/app_englishlearning/backend/app/senses.py:114)为每个用户和义项固定一条例句；v13 起不自动轮换例句。
10. [record_sense_review:225](/Users/hang/Desktop/app_englishlearning/backend/app/sense_learning.py:225)绑定用户、word_id、sense_id、example_id、attempt_id，按句中 target_form 判题，写历史与义项状态。
11. [advance_memory:104](/Users/hang/Desktop/app_englishlearning/backend/app/adaptive_memory.py:104)更新 FSRS、熟词候选与延迟确认；前端显示当前义项提示、例句挖空、反馈及其他义项。

## 1.2 当前数据模型和唯一键

| 对象 | 当前设计 | 实际含义 |
|---|---|---|
| words | 整数 id；owner_id NULL 为公共词、否则为私有词 | 拼写词头容器，兼有第一义项的扁平展示字段 |
| words 唯一键 | COALESCE(owner_id,0), word | 同一范围内按原始拼写查重，SQLite 此索引不是不区分大小写的词汇身份键 |
| word_senses | 整数 id；UNIQUE(word_id,sense_key) | 一个带词性的义项，有独立释义与 active 状态 |
| sense_examples | 整数 id；UNIQUE(sense_id,sentence) | 属于该义项的例句；答案由 target_form 指定 |
| sense_srs_state | PRIMARY KEY(user_id,sense_id) | 当前主要复习单位 |
| adaptive_memory | PRIMARY KEY(user_id,sense_id) | FSRS card_json、熟词候选、确认次数及首次/最近独立确认时间 |
| review_history | 整数 id；保留 word、sense、example、attempt 关联 | 作答事件，包括正确、独立性、提示、耗时和预测概率 |
| user_sense_examples | PRIMARY KEY(user_id,sense_id) | 当前用户的固定例句 |
| srs_state | PRIMARY KEY(user_id,word_id) | 旧整词学习状态，保留并尝试无歧义迁移 |
| word_list_memberships | list_id、word_id | 词库按整个词头组织，尚不能单独限定一个词性单位 |

[迁移结构](/Users/hang/Desktop/app_englishlearning/backend/app/migrations.py:283)与[现有 words 唯一索引](/Users/hang/Desktop/app_englishlearning/backend/app/migrations.py:563)是实施依据。

导出阶段 normalize() 做 trim、casefold、弯引号归一，并保留显示用 I；数据库词头更新用精确 word 匹配。查询接口用 lower(word) 查找，且只返回一条优先私有的 words 记录。历史上大小写变体或公共/私有同词头可以同时存在，不能直接增加一个大小写无关唯一索引并删除冲突行。

POS 主要保存在 word_senses.part_of_speech，是中文展示字符串，部分保留英语原词性，如 plural noun、copular verb。words.part_of_speech 只是第一义项的兼容字段。没有独立的规范 POS enum。

sense_key 来自原词典 lexid / id；缺失时使用包含词头、词性、释义的哈希。现有学习身份由数据库整数 sense_id 承担。定义哈希会随源解释变化；词典 id 也没有跨版本稳定性保证。

当前没有统一的 first_seen、last_review 或 mastery 百分比列。首次接触可从 study_attempts.created_at 与 review_history.review_time 推导，最近复习见历史及 FSRS card_json；streak 按用户本地日期从历史计算，Mature 保存在 status，ease 对应 easiness_factor。迁移应保留这些原始事件和字段，而不是假设它们都在 words 上。

## 1.3 当前能力审计

**已经可以做到：**

- 同一词头收录多个 POS 和义项，每个义项独立复习。
- 同一义项的例句包含不同语法词形，并按实际句中词形出题和评分。
- say 的“说”义项已包含 say、says、said、saying；它们不是四个新的义项状态。
- absolute 与 absolutely、hard 与 hardly 已各自收录和独立学习。
- building、meeting、painting、beginning 的名词用法，以及 walking 的“活的”形容词用法，已有独立词条。
- 保留历史、旧状态、管理员修正、固定例句，更新词库时归档旧内容。

**做不到：**

- 输入 said / says / saying 后反查 say；当前查询只查 words 拼写。
- 对新增或历史独立词头进行带证据的屈折归并。
- 表达同一 surface 同时是词形和独立词汇的查询结果。
- 展示派生关系或区分屈折、派生、其他关联。
- 判断两个不同词头的义项是否相同、能否安全共用调度。
- 将按词头的词库归属、静音、基础词过滤统一映射到规范词汇单位。

**已有但未充分利用：**

- word_forms.json 有 9,530 个词头的 POS 分组词形，包含不规则变化。
- 42,122 条 catalog 例句中有 11,857 条 target_form 与词头拼写不同。
- 稳定 sense_key、多个 POS、原词典义项结构、例句对应关系。
- 当前“related-exposure”其实记录同词头其他义项的曝光，尚不是 word family 功能。

**当前丢弃或压平的信息：**

- 原词条 id、词性区块 id、原始 POS、同形异义词条边界。
- 语法引用中的目标条目/区块、非可学词条的语法指向。
- 检索别名与明确屈折字段的证据差异；word_forms 合并英汉来源后只剩标签。
- NOAD 的 DERIVATIVES 区块、词源、多数用法/领域标签及结构化交叉引用。
- 独立短语、短语动词等子词条没有作为独立数据集导出。
- 没有可接受例句的义项不会进入可学 catalog。
- 有可学英汉义项时，未对齐的英文义项通常不会全部追加；不能把 catalog 当作词典全部义项全集。

**需要特别纠正的现状：**

says、said、saying 当前不在源词头快照中，也不在可学 catalog；因此目前并没有四套公共复习曲线。缺口首先是可用查询及规范身份，而不是删除当前 say 的三个公共重复项。

saying 名词在真实英汉记录中有“谚语、警句、格言”和例句 as the saying goes，现有解析器能接受。它未进入当前 catalog 的主要原因是源词头范围，而不是词典没有这个名词。英文记录的相关义项没有通过现有筛选的例句；不能假设每部词典都给出完整可学例句。

# 2. 当前字典能力

## 2.1 实际数据源和“接口”层次

这里有三个不同层次：

1. **源词典读取接口**：Mac 本地 AppleDictBin 记录，提供词条检索名和 HTML，不是返回 lemma / derivation JSON 的在线服务。
2. **项目导出格式**：catalog v2 只含词头、兼容字段、senses、memberships；word_forms v1 只含词头 → POS family → 拼写 → 语法标签。
3. **部署后的查询接口**：[GET /api/dictionary/{word}](/Users/hang/Desktop/app_englishlearning/backend/app/main.py:286)仅查询 SQLite，返回 word、source、available、definition；definition 包含兼容扁平字段和 senses。它不读取 Mac 词典，也不返回词形或词族。

NGSL CSV 的 Lemma 列只被用作词头；SFI Rank 用于排序，不是系统已经有了词形到 lemma 的语义映射。ECDICT 路径用于考试标签及源词头，当前词义构建不依赖其词汇字段；仓库未带该原始 CSV，本轮不能确认它还有哪些实际字段。

译典通繁体英汉词典由 supplement_chinese_from_dictionary.py 可选读取，用于保守中文补充，不是主词汇身份来源。

## 2.2 字典能力矩阵

“源提供”仅表示本机实际记录的结构；“已导出/使用”描述当前程序。

| 信息 | 实际源提供情况 | 当前程序导出 / 使用 | 支持归并或派生的结论 |
|---|---|---|---|
| lemma / 原型词 | 有词头；部分语法引用明确指向基础条目；没有统一 lemma 字段 | 词头保留；语法引用未保留 | 可支持部分高证据映射，不能从任意拼写直接取得唯一 lemma |
| base form | 与词头、屈折区块、语法交叉引用组合表达 | 只有前向词形表 | 应增加按条目和 POS 限定的反查 |
| POS | ps / pos 区块及 d:pos | 保留显示 POS；规范化有缺陷 | 足够作为必要条件，单独不足以合并 |
| inflected form | infg / inf；以及混合检索别名 | 导出到 word_forms.json，主要用于提示 | 需要重建来源和区块作用域，不能直接信任全部 v1 记录 |
| plural | 实际有 mice → mouse、children → child，以及基础条目 plural 标签 | 前向表已含不规则复数 | 可用；要支持多变体及义项限定 |
| past tense | 实际有 said → say、went → go 语法引用 | 前向表已含 said、went | 明确语法引用可高置信处理 |
| past participle | 实际有 said、gone 等 | 前向表保留标签 | 词形身份可明确，独立形容词义项仍需保留 |
| present participle | 有部分明确标签；许多规则形式来自索引加规则 | 当前以 ing 标签保存；句法细分是 heuristic | 词形标签不证明所有同拼写义项都是屈折 |
| comparative / superlative | infg、语法标签、局部比较级引用或来源说明；覆盖不统一 | good 的 better/best 等已导出 | 必须保留 good / well 与不同 POS 的多候选 |
| derived words | NOAD 有带 t_derivatives 的子词条区块，覆盖不完整 | 明确跳过子词条；未导出关系 | 可以提取显式关系，但不能承诺任意派生对都存在 |
| related words | 有 x-dictionary 交叉引用，但目的可能是语法、参见、拼写变体、词源等 | 多数结构未保留 | 需要先识别引用类型，不能全部当成词族 |
| word family | 未发现统一、完整的词族字段 | 无 | 可从经验证的派生/词汇化关系构建局部图 |
| root | 词源 prose 可能提到历史词根；无通用结构化词根字段 | 无 | 不适合直接驱动学习归并 |
| affix | 某些解释可涉及构词，但没有统一词缀分解字段 | 无 | 规则只能产候选，不作为合并证据 |
| etymology | NOAD 有 etym 区块；本轮英汉记录未发现同等结构 | 未导出 | 可留作可选证据/展示，不用于“共同词根即合并” |
| pronunciation | ph、IPA、方言、部分 soundFile 属性 | 导出词头音标；前端使用浏览器 TTS | 尚无完整词形发音模型，也没有导出词典音频 |
| frequency | 源词表有排名；未发现当前导出可用的词典频率字段 | memberships.position 用于排序 | 不能作为词形或语义等价证明 |
| sense | 原词典意义组与子义项 | 可学部分已保留 | 项目最值得复用的基础 |
| sense ID | 英汉 lexid、英文 id；缺失时哈希 | 保存为 sense_key，数据库另有整数 id | 支持稳定重导入，但须加入来源版本/预期内容校验 |
| definition | 中英文释义 | 按义项保留 | 可做确定性匹配及人工审核，不用相似度自动合并 |
| usage label | reg、lbl、lg、gg、领域/语法标记等实际存在 | 少量语义提示进入 definition_en，主要标签未单独保留 | 能辅助阻止错误合并，首版不必做完整标签 UI |
| phrase / collocation | 独立短语区块、短语动词、例句/模板 | 独立区块多被排除；部分搭配以例句文本保留 | 尚无完整独立 phrase/collocation 数据模型 |
| examples | 有义项所属例句与部分中文翻译 | 保存 sentence、target_form、translation_cn | 足够支持现有学习；未通过筛选的用法仍可能缺失 |

原始英汉记录还出现交叉引用链接上的 band 属性；本轮未确认其定义，不能把它解释为英文词频。现有例句筛选会排除 sb/sth 模板与过短内容，但 catalog 仍有诸如 a small hill/room/car 的片段，不能声称每条都经过完整句法验证。

## 2.3 本轮直接确认的源事实

- said 的英文词条同时含动词“say 的过去式/过去分词”引用和独立形容词义项。英汉词典也有“上述的”。因此连 said 都不能整词无条件重定向。
- saying 有独立名词记录。
- gone 有语法引用，也有多个形容词和介词义项。
- better 在 NOAD 有两个同拼写词条；其中一个含形容词、副词、名词、动词，另一个是 bettor 的拼写变体。
- well 与 real 都有多个原词典条目；词头加 POS 仍不足以永久区分所有同形异义。
- absolute 的 DERIVATIVES 区块列的是 absoluteness，本轮没有找到 absolute → absolutely 的显式关系；absolutely 有自己的独立词条。
- real → really、actual → actually、eventual → eventually、probable → probably 在抽样的这些记录中也没有完整显式父子关系。不能把拼写规则包装成“字典提供的派生关系”。

absolute → absolutely 首版可通过小型人工核对映射可靠补足；它的证据类型应是 reviewed_override，不能标成 dictionary_explicit。

# 3. 当前问题

## 3.1 身份层级缺失

words 把拼写容器和词汇身份合在一起。虽然 word_senses 已区分多词性和多义项，但没有“哪一个词性单位的哪个词形”的明确链接。

只增加 words.lemma_word_id 会使 said 的形容词、saying 的名词、walking 的形容词都跟着整个拼写被重定向。该方案不推荐。

## 3.2 现有词形表不宜直接用于自动归并

[POS 归类函数](/Users/hang/Desktop/app_englishlearning/backend/app/word_forms.py:26)按子串匹配，并先检查 verb。实际运行结果是：

~~~text
pos_family("adverb") = "verb"
pos_family("副词") = "adverb"
~~~

所以导出中的英文 adverb 会污染动词 family。当前 word_forms.json 可见：

- well 的 better / best 出现在 verb 分组。
- small、hard、late、real、good 等出现需要复核的“动词”词形。
- 表没有保留每个映射是明确屈折标签、哪部词典的哪一词性区块，还是全条目别名加规则生成。

[inflections_from_root](/Users/hang/Desktop/app_englishlearning/backend/scripts/dictionary_senses.py:115)还会把条目范围的 aliases 应用于多个 POS family。这对提示尚可保守容忍，对身份归并会扩大风险。

扫描得到 **615 个当前可学词头同时也是其他词头的词形候选**，另有 **27 个词形拼写指向多个基础词头**。这些只是候选规模，不是可以自动合并的条目数量。

## 3.3 词形不等于义项等价

知道 better 是 good 的比较级，不代表 better 的所有名词、动词或健康状态义项都能合并到 good。知道 saying 是 say 的 -ing 形式，不代表其“格言”义项可删除。

POS 相同、释义近似、共享例句或共享词根，都不能单独证明两个完整词条等价。

## 3.4 查询、词库和学习控制尚未跟随规范身份

- 查询只精确匹配词头，says / said 当前返回 available=false。
- 词库归属按 words，不能限定一个同拼写词的动词单位。
- 静音和基础 600 词过滤按词头拼写，未处理已验证的屈折别名。
- 熟练度聚合按 words 及其全部 active senses。
- 更多词义曝光只覆盖同一 word_id，未来展示派生词会额外泄露相关词答案。

## 3.5 当前固定例句与“偶尔测试别的词形”冲突

v13 明确为用户固定学习例句，现有测试保证刷新、换词库、复习时句子不自动换。随机轮换 said / saying 会改变这个已实现的产品行为。首版应保留固定例句；轮换作为后续单独功能验证。

# 4. 推荐的数据模型

## 4.1 选择与取舍

| 方案 | 改动与风险 | 结论 |
|---|---|---|
| 在 words 加唯一 lemma 指针 | 改动少，但无法表达多身份，容易整词误合并 | 不采用 |
| 新建完整 Lemma / Lexeme / Morpheme / Etymology 数据库 | 语言学覆盖大，但需要重建词库与大量调度迁移 | 当前不需要 |
| 保留 words / senses / 调度，补三个词汇身份表 | 复用现有进度，能表达多 POS、词形和派生，便于逐步增强 | 推荐 |

**用户复习单位继续是用户 × 规范义项，不改为整 lemma，也不改为每个词形。**

- words：兼容的拼写/详情容器。
- lexical_units：词汇单位，例如 say[VERB]、say[NOUN]、saying[NOUN]。
- word_forms：属于特定词汇单位的表面形式。
- word_senses：该单位的可学义项。
- lexical_relations：两个独立单位之间的派生、词汇化或其他经验证关系。
- sense_srs_state / adaptive_memory：每个规范义项一份用户状态。

这里的 lemma 就是 lexical_unit 的 canonical headword。不再增加一个仅保存相同字符串的 lemmas 表。

## 4.2 Phase 1 新表

下列是推荐的 schema 草案，字段名可直接使用；SQL CHECK 的完整枚举在实施时与 Python/TS 类型统一。

~~~sql
CREATE TABLE lexical_units (
  id INTEGER PRIMARY KEY,
  owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  word_id INTEGER REFERENCES words(id) ON DELETE RESTRICT,
  unit_key TEXT NOT NULL,
  headword TEXT NOT NULL,
  normalized_headword TEXT NOT NULL,
  pos_group TEXT NOT NULL,
  identity_status TEXT NOT NULL
    CHECK(identity_status IN ('verified','legacy','needs_review')),
  provenance_json TEXT NOT NULL DEFAULT '{}',
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE UNIQUE INDEX idx_lexical_units_scope_key
  ON lexical_units(COALESCE(owner_id,0),unit_key);
CREATE INDEX idx_lexical_units_surface
  ON lexical_units(normalized_headword,pos_group);

CREATE TABLE word_forms (
  id INTEGER PRIMARY KEY,
  lexical_unit_id INTEGER NOT NULL REFERENCES lexical_units(id),
  spelling TEXT NOT NULL,
  normalized_form TEXT NOT NULL,
  form_type TEXT NOT NULL,
  scope_sense_id INTEGER REFERENCES word_senses(id),
  verification TEXT NOT NULL CHECK(verification IN ('verified','candidate','rejected')),
  evidence_kind TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE UNIQUE INDEX idx_word_forms_identity ON word_forms(
  lexical_unit_id,normalized_form,form_type,COALESCE(scope_sense_id,0)
);
CREATE INDEX idx_word_forms_reverse ON word_forms(normalized_form,verification,active);

CREATE TABLE lexical_relations (
  id INTEGER PRIMARY KEY,
  from_unit_id INTEGER NOT NULL REFERENCES lexical_units(id),
  to_unit_id INTEGER NOT NULL REFERENCES lexical_units(id),
  relation_type TEXT NOT NULL,
  verification TEXT NOT NULL CHECK(verification IN ('verified','candidate','rejected')),
  semantic_transfer_allowed INTEGER NOT NULL DEFAULT 0,
  provenance_json TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  UNIQUE(from_unit_id,to_unit_id,relation_type),
  CHECK(from_unit_id != to_unit_id)
);

ALTER TABLE word_senses ADD COLUMN lexical_unit_id INTEGER REFERENCES lexical_units(id);
~~~

关键约束：

- word_id 可空，允许显示“词典有相关词，但当前没有可学例句”的元数据；不得伪造例句来满足 words 导入。
- unit_key 是稳定身份键，不使用“normalized spelling + POS”作为全局唯一身份；必须容纳 homograph/source-entry 区别。
- 旧数据先按 word_id 与规范 POS 建立 legacy 容器，仅增加分组关系。无法判断同形异义时保留 legacy / needs_review；不合并义项或进度。
- 同一 surface 可对应多个 word_forms、多个 lexical_units；没有全局唯一 surface。
- scope_sense_id 允许词形仅适用于一个明确义项，例如不同含义有不同复数。为空才表示整个单位的语法形式。
- past 与 past_participle 可是同一 spelling 的两条标签，反查时按 unit 汇总。
- 关系的方向和类型明确；屈折放 word_forms，不混入 related 关系。
- 学习权限按 owner_id 验证，私有单位与公共单位不能因为同拼写就全局合并。
- provenance_json 至少记录字典包 id/version、record id、grammar-block id、源 sense key、证据节点/标签、源指纹、resolver version、必要的预期释义/POS。

relation_type 首版使用 derived_adverb、derived_noun、derived_adjective、lexicalized_from、related；候选不自动展示为已确认词族。word family 可从 verified 派生/词汇化边生成局部图，不必增加 family 表。

## 4.3 Phase 2 调度别名与词库归属

~~~sql
ALTER TABLE word_senses
  ADD COLUMN canonical_sense_id INTEGER REFERENCES word_senses(id);

CREATE TABLE lexical_unit_list_memberships (
  list_id TEXT NOT NULL REFERENCES vocabulary_lists(list_id),
  lexical_unit_id INTEGER NOT NULL REFERENCES lexical_units(id),
  position INTEGER NOT NULL,
  source_spellings_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(list_id,lexical_unit_id)
);

ALTER TABLE review_history ADD COLUMN presented_form TEXT;
~~~

- canonical_sense_id NULL 表示自身为规范义项；非 NULL 是经过审核的同义项别名。
- 不能指向自身、形成环、指向另一个别名，或越过用户数据可见范围。服务层显式校验。
- 新的纯屈折输入直接链接既有单位，通常不需要再创建一个 word_senses 别名。canonical_sense_id 主要服务历史重复项。
- 从现有词头 membership 展开所有现有单位，保留原“词头全部义项”的行为。新增屈折来源只关联已确认的单位，不把 book 的名词复数输入误扩展成 book 的所有动词义项。
- 原词表快照及 source_word_count 不改；规范学习统计去重，排序使用来源最小 position，并保留原输入拼写。
- presented_form 只记录今后实际考到的词形。旧值保持 NULL；管理员可能修改过例句，不能把当前 target_form 冒充历史答案快照。

## 4.4 不需要新增的字段 / 状态

第一版不新增独立 lemma mastery、word-form SRS、词根/词缀表、词源树、家族 SRS、重复 meaning_id、另一个 ease/interval/mature 字段。

现有 sense_id、sense_key、target_form、review_history、adaptive_memory、状态字段继续使用。词形掌握先由未来 presented_form 事件汇总为“该形式的实际作答证据”，不要宣称形成精确识别概率。

# 5. 分类算法

## 5.1 分类粒度与输出

输入必须是 word + **按来源和词性拆分的词典信息**，输出是一个结果列表，不是 word → 一个 enum。

~~~text
Resolution {
  surface,
  entries: [
    {
      classification: INFLECTION | DERIVED | LEXICALIZED_FORM | INDEPENDENT | UNKNOWN,
      lexical_unit_key,
      canonical_unit_key?,
      source_entry_key,
      source_block_key?,
      sense_keys: [],
      form_types: [],
      relations: [],
      evidence: [],
      action: LINK_FORM | CREATE_INDEPENDENT | KEEP_EXISTING | METADATA_ONLY | NEEDS_REVIEW
    }
  ]
}
~~~

saying 可以同时返回 INFLECTION → say[VERB] 和 LEXICALIZED_FORM → saying[NOUN]。不存在“saying 整体必须等于/不等于 say”的全局决定。

## 5.2 证据优先级

1. 原词性区块内的明确屈折字段，或受控语法引用 + 目标条目/区块。
2. 字典原词性、义项与独立定义，用于分离身份和阻止错误归并。
3. 显式 DERIVATIVES 区块等关系。
4. 已验证的项目映射与有来源校验的人工 override。
5. 词典检索别名加形态规则，只生成候选；作用域不足时不自动合并。
6. 纯字符串变化，只生成 UNKNOWN 候选。

优先级是按信息类型决定用途：POS/sense 是阻止误合并的证据，不是“POS 相同即可合并”的授权。

必须先修复 POS：使用明确映射或 token/完整标签规则，把 adverb、verb 分开；保留原始标签，未知归 OTHER。不能继续用包含 verb 子串的判断。

## 5.3 Decision tree / pseudocode

~~~python
def classify_entry(surface, raw_entry, context=None):
    # 必须保留 source-entry、homograph、POS block、sense 边界
    slices = split_entry_by_grammar_and_sense(raw_entry)
    results = []

    for entry in slices:
        pos = normalize_pos(entry.raw_pos)
        explicit_form_ref = extract_scoped_inflection_reference(entry)
        autonomous_senses = extract_non_grammatical_senses(entry)
        derivation = extract_verified_derivation(entry)

        if explicit_form_ref:
            target_units = resolve_target_entry_and_block(explicit_form_ref)
            if unique_compatible_target(target_units, pos):
                # 语法身份只连接词形，不复制基础词各义项
                results.append(link_inflection_identity(entry, target_units[0]))
            else:
                results.append(unknown(entry, reason="ambiguous_or_missing_target"))

        # 一个区块可能同时含语法说明与独立词义，两部分分别保留
        for sense in autonomous_senses:
            if verified_lexicalized_overlap(surface, pos, sense):
                results.append(create_independent(sense, kind="LEXICALIZED_FORM"))
            elif derivation and relation_scope_matches(sense, derivation):
                results.append(create_independent(sense, kind="DERIVED"))
            else:
                results.append(create_independent(sense, kind="INDEPENDENT"))

        if not explicit_form_ref and not autonomous_senses:
            candidates = verified_project_forms(surface, pos)
            if unambiguous_attested_context(candidates, context):
                results.append(link_inflection_identity(entry, candidates[0]))
            else:
                results.append(unknown(entry, candidates=rule_candidates(surface)))

    return reconcile_conflicts_without_whole_word_merge(results)
~~~

实际解析需写两个有限适配器，分别对应 NOAD 与英汉 HTML：

- “past / past participle of”语法说明必须与同区块 x-dictionary 目标链接一致。
- 英汉 past tense / plural + xr 引用需要解析条目 id、xpointer 区块；不要只抓链接 title。
- 语法引用没有普通 definition 时保存元数据，而不是因没有例句丢失词形关系。
- infg 的标签与 inf 节点按实际语法区块绑定；derivative 子条目不作为主词屈折。
- 已知形态规则可给明确 infg 节点补“比较级/第三人称”标签；只有全条目 aliases 的形式仍需额外确认或人工审核。
- 未识别的 HTML、来源版本变化、歧义目标、普通参见链接都走 UNKNOWN。
- 词源区块中的“历史语言的过去分词”不属于当前英语屈折身份，例如 absolute 的拉丁来源不能触发 absolute → absolve 归并；语法引用适配器必须排除 etym 和非当前词性区块。
- 英文定义的通用相似度、embedding 或语言模型猜测，不作为自动 merge 判据。

## 5.4 MERGE_AS_INFLECTION 的附加门槛

“识别一个词形”与“合并两个已有学习义项”是两个动作。后者还必须同时满足：

1. 来源或人工审核确认是同一词汇单位和匹配 POS。
2. 明确对应同一个意义，不只对应同一个词头。
3. 无未处理的独立义项。
4. 例句/答案形式和管理员修正有效。
5. 用户归属、状态与活动轮次不存在未处理冲突。
6. 合并计划可预览、可回滚。

不满足时可有 verified word-form 链接，但**不合并已有 sense 的状态**。

hard → hardly、late → lately、near → nearly 不走屈折。即便建立派生关系，也保持独立学习；“同 POS”不是特殊通行证。

# 6. 导入流程修改

## 6.1 当前入口必须保持真实

当前 [POST /api/words 与 /api/words/import](/Users/hang/Desktop/app_englishlearning/backend/app/main.py:322)固定 410，用户导入 UI 已停用。

第一版修改公共离线构建和数据库导入，**不顺手恢复用户导入**。下述统一 resolver 可供未来新增入口复用。

## 6.2 公共离线构建

1. 规范化源输入，保留 source_surface、list_id、rank；不要据词尾直接改词头。
2. 读取词典元数据、主词条与相关语法引用。范围包含源词、seed、已配置的审核映射，以及关系需要的有限目标；不需要把全部词典都做成可学库。
3. 对每个原词条保留来源、homograph 和 POS block，解析主义项、明确屈折、语法引用、派生子条目。
4. 分类得到多个身份。独立词义走现有例句匹配与跨词典对齐；纯语法身份链接规范单位。
5. 保留未有例句的词汇/关系元数据，但不创建学习卡。
6. 已验证屈折输入只加入规范单位的词库归属；多候选且无上下文时列入审核清单，不选择第一个。
7. 输出兼容 catalog v2 和增强 word_forms v2，以及 UNKNOWN / 冲突 / 无例句报告。
8. 绑定两个输出文件的校验和；先校验整包，再执行数据库更新。不能出现“新词义配旧形态证据”的混合部署。

saying[NOUN] 第一版可加入明确的补充词头集合，使用实际英汉名词义项和例句。该独立名词不借用 say 的例句，不因加关系就更换用户原有词库选择。

## 6.3 SQLite 保存

1. 校验格式版本、来源指纹、sense_key 引用、所有 POS、target_form 配对及作用域。
2. 保持现有 words 和 sense/example id 的更新规则。
3. 写 lexical_units 和 sense bindings；旧身份无法确认时保持 legacy。
4. 写 word_forms 和 lexical_relations，verified/candidate 分开。
5. 写规范单位 membership；归档上游移除的关系，保留引用和历史。
6. 应用管理员修正；重新验证修正后的词形链接，不能让形态导入覆盖修正。
7. 只在严格义项映射成立且状态符合迁移条件时设置 canonical_sense_id。
8. 更新各自 fingerprint；整个内容更新事务成功才提交。

seed 与私有旧词也接入 lexical_units；只有平面词性、释义、例句的旧私有词，不自动推断 lemma。

## 6.4 未来恢复用户“添加单词”时

建议 resolver 先返回可预览结果：

| 用户输入 | 应有行为 |
|---|---|
| said，选择动词身份，已有 say | 关联 say[VERB]，显示一次“say 的过去式/过去分词”；不再创建 said 动词的一套 SRS |
| said，无 POS/上下文 | 同时呈现 say 动词词形与 said 形容词身份，保留歧义 |
| saying，选择动词形式 | 关联 say[VERB] |
| saying，选择“格言” | 创建/关联 saying[NOUN]，独立义项学习 |
| absolutely，已有 absolute | 关联/创建 absolutely[ADV]，独立学习，建立已审核派生关系 |
| 完全未知拼写或无可靠例句 | 保留 UNKNOWN 或不可学元数据，不伪造词义和例句 |

关联公共单位还必须持久化用户的“单词加入”选择。当前只有词库选择与私有词可见性，没有逐词公共收藏/入队表。若未来恢复此功能，应增加用户与单位的 enrollment 表并扩展 selected_filter；否则“关联成功”不代表它会出现在用户新词队列。第一版不需要这个表。

# 7. 复习系统修改

## 7.1 规范义项继续驱动调度

最终关系：

~~~text
lexical_unit: say [VERB]
  forms: say / says / said / saying
  canonical sense 1: 说
    user × sense 1 → 一份 sense_srs_state + 一份 FSRS card
    examples 可以出现上述不同词形
  canonical sense 2: 宣称
    user × sense 2 → 另一份状态

lexical_unit: saying [NOUN]
  canonical sense: 格言
    → 独立状态

absolute [ADJ] → absolutely [ADV]
  → 两边分别按自己的义项调度
~~~

这不是把 say 的 14 个现有义项压成一条曲线，而是避免每个拼写再复制这 14 个义项。

所有新出题只用 active 的规范义项。历史别名不参与新词、到期、成熟度重复计数；查询可以展示它们与规范义项的对应。

如果以后允许从已归并别名的例句出题，必须验证 example 的原始 sense 与当前 canonical sense 的已审核关系。第一版继续从规范义项现有有效例句出题，避免放宽当前 pair 校验。

评分继续严格匹配句中 target_form。题目期待 said 时，填 say 仍然错误；共享曲线不意味着答案词形可以随意替换。

## 7.2 当前首次答对与成熟机制

实际代码不是“首次答对即成熟”：

- 全新、无历史、无 exposure、具备新提示追踪且独立答对的义项，可成为 known_candidate，并设 stability=30 天。
- 第一、第二次确认间隔分别有 30 / 60 天上限。
- 独立确认相隔至少 24 小时才增加 confirmations。
- Mature 要求至少 3 次独立确认、稳定性至少 180 天、间隔至少 180 天、首个确认距今至少 90 天；评分层还要求过去 180 天无错误。
- 成熟 FSRS 义项继续进行 180 天检查。
- 朗读、看答案、纠正都不能冒充独立成功；非独立结果进入至少 20 分钟后的巩固。
- 个人遗忘速度校准只使用可用的延迟、无辅助样本；当前门槛为至少 150 个样本、20 个义项、30 天跨度。
- 旧 Mature 且未有 adaptive_memory 的记录有兼容保留逻辑，迁移不能无意取消它。

相关代码：[advance_memory](/Users/hang/Desktop/app_englishlearning/backend/app/adaptive_memory.py:123)、[成熟门槛](/Users/hang/Desktop/app_englishlearning/backend/app/adaptive_memory.py:168)、[评分与状态更新](/Users/hang/Desktop/app_englishlearning/backend/app/sense_learning.py:277)。

**第一版不改变这些门槛。** absolutely 首次独立答对可以通过它自己的现有熟词机制快速延长间隔；学过 absolute 不会替它生成确认、SRS 或 Mature。

## 7.3 word family familiarity

当前熟词先验已经很强，再给派生词叠加家族奖励可能重复计入同一信息。

推荐首版仅计算关联词的学习状态用于词族展示或出题排序，不修改 FSRS 稳定性。后续只有在有实际延迟答题数据支持时，才尝试有上限的派生先验：

- 只针对 verified、语义可预测关系。
- 目标自己的独立作答与成熟门槛始终保留。
- 不复制 base 的 card_json、interval、confirmations 或 Mature。
- 不绕过历史错误、提示/exposure 或旧追踪版本的限制。
- hard/hardly、late/lately 等语义变化关系不使用正向迁移先验。
- 模型版本更新并单独验证，不能把产品设定说成已校准的记忆概率。

## 7.4 词形认识与偶尔测试

最小版本：每份规范义项曲线处理其实际例句词形，不增加词形专属 SRS。

可选增强：通过 future presented_form 汇总某形式答对、错误、是否独立、最近见过时间。这是观察记录，不是默认“学过 lemma 就掌握全部不规则形”。

偶尔换形式只能使用**同一规范义项已有且可靠配对的真实例句**，不能将原句机械替换为另一个时态。若另行开启轮换：

- 初次学习和待纠正/待巩固轮次仍固定。
- 在新复习轮次开始前选句；刷新和并发恢复保持同 attempt/example。
- 不能因换形式增加同日确认。
- 保留固定模式为默认，并修改对应 v13 回归测试的预期范围。

不建议作为第一版必要内容。

# 8. UI 修改

当前没有独立 WordDetail 页面。实际详情入口是 [StudyTools.tsx](/Users/hang/Desktop/app_englishlearning/frontend/src/StudyTools.tsx:16)的“更多词义”对话框，学习主体是 [StudyCard.tsx](/Users/hang/Desktop/app_englishlearning/frontend/src/StudyCard.tsx:29)。

最低必要改动：

1. 更多词义内按 lexical_unit 分组，保留词头、POS、当前义项和原有例句。
2. 有数据时显示“屈折形式”“派生词”“其他关联”三个独立区域；无数据区域不显示。
3. 派生关系展示带词性方向，如 absolute [形容词] → absolutely [副词]。
4. saying[NOUN] 展示自己的“格言”义项；另以单独行显示 say [动词] → saying [-ing 形式]，不能混成名词的解释。
5. StudyCard 保留原有 POS + answer_form_label；不增加常驻 lemma、置信度、来源和说明小字。
6. 第一版不加新标签页或复杂词族树，不在登录或学习页面增加教程性小字。
7. 关联词点击仍在当前弹窗展开；iOS 上保留滚动、关闭、返回、输入框及未完成轮次。

**曝光必须处理：**看到词形列表或关联词拼写本身可能泄露答案。答题前主动打开详情需要先记录当前题 answer_exposed，以及实际显示的相关单位规范义项 exposure；答对后的查看不改变已完成题评分，但仍影响相关新词的首次熟词先验。

[record_related_exposure](/Users/hang/Desktop/app_englishlearning/backend/app/sense_learning.py:208)现在只覆盖同 word_id 其他义项，要扩为“实际返回的 canonical sense 集合”。不能因为用户展开一个派生词，就标记整个连通词族所有未展示词汇。

GET dictionary 保持读接口。学习过程中展开更多关联详情应走带 attempt_id 的 POST，先写 exposure 再返回内容；不要为 GET 偷加写副作用，也不要仅在前端标志已曝光。

# 9. 数据迁移方案

## 9.1 Schema 与内容迁移分开

当前代码 schema 最新 v13。第一阶段新增结构用 v14；后续规范调度/词库适配再递增，不跳过 v2 到 v13 的已有迁移。

现有 [run_migrations](/Users/hang/Desktop/app_englishlearning/backend/app/migrations.py:20)按版本迁移，[_backup](/Users/hang/Desktop/app_englishlearning/backend/app/migrations.py:425)用 SQLite backup API 包含 WAL 数据。

实施顺序：

1. 读取线上实际版本、DATA_DIR、数据数量与完整性；创建一致备份。
2. 在临时 DATA_DIR 的副本升级并验证；禁止测试导入生产路径。
3. 第一阶段只新增表/列与 legacy 分组，逐行核对原学习数据不变。
4. 根据真实源证据产生**独立的内容归并计划**，列出每个源/目标 unit、sense、example、用户状态、覆盖修正、静音和词库影响。
5. dry-run 检查后，在内容事务中应用批准的安全映射。
6. 保留计划、前后快照、对应来源指纹及回滚信息。

迁移 DDL 使用逐条 execute。不要在显式事务中用 executescript。

## 9.2 哪些可自动处理

| 情况 | 自动处理建议 |
|---|---|
| 新屈折输入，基础单位已存在，只有明确语法身份 | 直接链接 word form，不创建新义项/状态 |
| 两个已存在候选义项均无学习/提示/轮次/修正记录，且同义项对应已验证 | 可将重复项设为别名，只保留一个入队单位 |
| 只有一边有进度 | 仅在同义项、例句目标、所有状态及修正都有无歧义映射时，可转移到规范调度；任一条件不满足则保留原状 |
| 两边都学过 | 第一版不自动合并 |
| 平面私有词、义项无法对应 | 保持原状，NEEDS_REVIEW |
| 名词 saying、形容词 walking、独立意义的 said/gone | 独立保留；不能整词归并 |
| 派生词 | 仅加关系，学习历史与状态不动 |
| 大小写/来源冲突、多基础词、多个 homograph | 审核，不通过字符串选择规范项 |

**只有一边有进度**的安全转移细节：

- review_history 原 id、word_id、sense_id、example_id、review_time、答案与提示记录不改写。
- 原状态保留为迁移前快照；规范目标无状态时复制原 review/correct/wrong/lapse/ease/interval/due/status。
- adaptive_memory 保留 stability、difficulty、known_candidate、confirmations、first/last_independent_at；若换 sense_id，FSRS card_id 需显式更新，原 JSON 仍保留在快照中。
- user_sense_examples 只能指向规范义项有效例句。如果没有完全对应的例句，不进行自动转移。
- 不把 source example_id 原样塞到另一个 sense 的固定例句记录中。
- 待巩固队列保留真实 ready_at 和顺序；仅映射 id。
- 所有影响的未完成 attempt 结束，旧手机提交返回 409 并重新取题；不能悄悄把已签发题目换成另一义项。
- 旧状态不再参与活跃调度，但保持可审计；原记录不能因 CASCADE 删除。

## 9.3 两边都有历史时

检测用 word-form 证据、POS 和明确义项映射，**不使用词尾猜测、最大 correct_count 或最大 interval**。

首版推荐产生待处理计划并保留各自曲线。这是可维护的选择：错误归并的风险高于暂时重复。

后续专门的历史归并操作必须先保存可恢复的 merge receipt，至少含源/目标、原状态、固定例句、队列、exposure、修正引用、实际应用变更和审核人/时间。

冲突处理建议：

- 两边历史按原时间和 id 形成逻辑联合视图，不删除事件，不把同日多个成功伪造为多次延迟确认。
- 不平均两个 FSRS card_json，不把两边 confirmations 相加，也不任选最长间隔。
- 对可信追踪历史可研究按时间重放；旧 is_independent=NULL 的记录不能被假定为独立 Good。
- 默认选择保守的当前估计并明确预览：近期失败/待巩固优先、到期不晚于原两边最早到期、不因归并增大稳定性。
- Mature 不能做简单 OR；已有成熟状态保存在原状态快照，若合并后的有效状态需改变，必须在计划中明确列出，首版不自动降级或覆盖。
- 若无法同时保留有效固定例句与稳定状态映射，则不合并。
- 错误映射可撤销：恢复原别名关系、状态与固定例句；归并后新增的作答必须按新事件归属处理，不能简单拿旧整库备份覆盖之后的新学习。

## 9.4 统计与控制链必须同步

- streak 按原用户 review_time 计算，历史不删，日期不变。
- learned / mature / due 按 canonical senses 聚合，不再计别名第二份。
- 错误窗口、同日独立成功限制、熟词历史检查、曝光检查和校准取样，都要包含已确认别名的历史。
- 校准检查“至少 20 个义项”应按规范 id 去重，不能让别名抬高有效样本多样性。
- family relation 不参与任何掌握数量加成。
- “不再学习”维持同词头所有身份的原行为；say 的屈折身份跟随其规范单位，saying[NOUN]、absolutely 不随基础词静音。
- 旧 said 静音记录只能通过已确认的对应 sense/unit 解释，不能全局把所有 said 记录改写成 say；保留原拼写偏好和审计。
- skip_basic_600 对已验证屈折使用规范词头，对独立派生/词汇化单位继续按自己的词头判断。
- 现有“已学义项到期复习不因取消词库选择而消失”的行为继续保留。

# 10. 需要修改的文件

以下全部是当前 repository 已存在的真实文件。新增数据资源另在第 12 节说明。

| 文件 | 当前职责 | 推荐修改 |
|---|---|---|
| [backend/scripts/dictionary_senses.py](/Users/hang/Desktop/app_englishlearning/backend/scripts/dictionary_senses.py) | HTML 义项/POS/例句/屈折解析 | 保存来源条目和区块；解析语法引用、derivatives；保留独立词义，限制 alias 作用域 |
| [backend/scripts/build_vocab_bundle.py](/Users/hang/Desktop/app_englishlearning/backend/scripts/build_vocab_bundle.py) | 源词表、词典读取、义项对齐、输出 | 将学习内容与形态元数据分开；维护补充词头和归并报告；输出来源 manifest 和配套指纹 |
| [backend/scripts/export_word_forms.py](/Users/hang/Desktop/app_englishlearning/backend/scripts/export_word_forms.py) | 独立导出词形 | 输出 v2 证据、反查、unit/sense bindings、关系；保留 v1 projection；支持明确补充目标 |
| [backend/app/word_forms.py](/Users/hang/Desktop/app_englishlearning/backend/app/word_forms.py) | POS family、规则词形、提示 | 修 POS；新增规范化、分类/解析结果、反查、证据门槛与 lexical metadata 查询；函数接收 conn，避免数据库循环导入 |
| [backend/app/database.py](/Users/hang/Desktop/app_englishlearning/backend/app/database.py) | 初始化、catalog 导入、词库统计 | 导入形态元数据；独立 fingerprint；保持 id/修正；规范单位 membership 与统计 |
| [backend/app/migrations.py](/Users/hang/Desktop/app_englishlearning/backend/app/migrations.py) | v1–v13 升级与备份 | 增加下一版本的表/列与保守 legacy backfill；内容归并不塞进无条件 schema 迁移 |
| [backend/app/senses.py](/Users/hang/Desktop/app_englishlearning/backend/app/senses.py) | sense/example 保存与固定例句、旧进度 | 保存 unit binding；规范 sense 校验；详情返回按 unit 分组；不放宽例句归属 |
| [backend/app/sense_learning.py](/Users/hang/Desktop/app_englishlearning/backend/app/sense_learning.py) | 出题、评分、统计、曝光 | 规范义项过滤；历史别名范围；曝光精确集合；记录 presented_form；保持 attempt/答案校验 |
| [backend/app/adaptive_memory.py](/Users/hang/Desktop/app_englishlearning/backend/app/adaptive_memory.py) | FSRS、熟词先验、校准 | 历史/曝光/有效义项按规范映射；首版保留先验与成熟门槛 |
| [backend/app/study_tools.py](/Users/hang/Desktop/app_englishlearning/backend/app/study_tools.py) | 更多词义与反馈 | 返回 units/forms/relations；根据真实返回内容记录曝光；保持原反馈快照 |
| [backend/app/main.py](/Users/hang/Desktop/app_englishlearning/backend/app/main.py) | API 输入和路由 | 扩展 dictionary/meanings 响应与详情输入；保留退休导入接口 410 |
| [backend/app/learning_filters.py](/Users/hang/Desktop/app_englishlearning/backend/app/learning_filters.py) | 基础词及可学过滤 | 规范单位/词形感知，同时保留独立词汇 |
| [backend/app/muted_words.py](/Users/hang/Desktop/app_englishlearning/backend/app/muted_words.py) | 按拼写静音和恢复 | 兼容已验证别名；保持原偏好记录及跨设备轮次结束逻辑 |
| [backend/app/admin.py](/Users/hang/Desktop/app_englishlearning/backend/app/admin.py) | 内容修正与学习统计 | 修正后复核形态链接；规范计数；历史报告仍指向原内容 |
| [backend/app/content_overrides.py](/Users/hang/Desktop/app_englishlearning/backend/app/content_overrides.py) | 修正持久化与重导入 | 保证词形/关系刷新不覆盖已有修正；冲突送审核 |
| [frontend/src/types.ts](/Users/hang/Desktop/app_englishlearning/frontend/src/types.ts) | Card/Sense/Review/Stats 类型 | 加 lexical metadata 和可选兼容字段 |
| [frontend/src/StudyTools.tsx](/Users/hang/Desktop/app_englishlearning/frontend/src/StudyTools.tsx) | 更多词义/报告弹窗 | 词汇单位分组与三类关系区域；关联详情在原弹窗展开 |
| [frontend/src/StudyCard.tsx](/Users/hang/Desktop/app_englishlearning/frontend/src/StudyCard.tsx) | 当前/上一题共用卡片 | 必要时显示规范单位及答案形式；保留现有简洁提示与输入 |
| [frontend/src/App.tsx](/Users/hang/Desktop/app_englishlearning/frontend/src/App.tsx) | 轮次、提示、曝光、导航、统计 | 根据服务端 exposed_sense_ids 更新当前卡；保持 iOS 输入和上一题快照 |
| [frontend/src/index.css](/Users/hang/Desktop/app_englishlearning/frontend/src/index.css) | 学习页面与弹窗布局 | 最小关系区布局，适配窄屏和触控 |
| [backend/data/word_forms.json](/Users/hang/Desktop/app_englishlearning/backend/data/word_forms.json) | v1 词形快照 | 升为带证据的 v2，保留兼容前向词形 |
| [backend/data/vocabulary_catalog.json](/Users/hang/Desktop/app_englishlearning/backend/data/vocabulary_catalog.json) | 可学义项与例句 | 保持原键；只增加核实的独立词头/义项，不全量改写所有学习身份 |
| [backend/data/vocabulary_export_report.json](/Users/hang/Desktop/app_englishlearning/backend/data/vocabulary_export_report.json) | 覆盖/无例句报告 | 加分类、证据、歧义及建议归并统计 |
| [backend/tests/test_dictionary_export.py](/Users/hang/Desktop/app_englishlearning/backend/tests/test_dictionary_export.py) | 原字典解析测试 | 加真实区块结构、语法引用、多身份、源变化和 POS 修复回归 |
| [backend/tests/test_word_forms.py](/Users/hang/Desktop/app_englishlearning/backend/tests/test_word_forms.py) | 词形提示与 ing 提示 | 加分类、反查、歧义、规则降级、irregular 测试 |
| [backend/tests/test_senses.py](/Users/hang/Desktop/app_englishlearning/backend/tests/test_senses.py) | 学习与迁移集成 | 加规范单位、归并计数、独立义项与历史保留 |
| [backend/tests/test_adaptive_memory.py](/Users/hang/Desktop/app_englishlearning/backend/tests/test_adaptive_memory.py) | FSRS/成熟/曝光 | 验证派生不复制成熟、别名不抬确认、家族曝光 |
| [backend/tests/test_study_tools.py](/Users/hang/Desktop/app_englishlearning/backend/tests/test_study_tools.py) | 详情与反馈 | 加跨单位详情权限及精确曝光 |
| [backend/tests/test_basic_words.py](/Users/hang/Desktop/app_englishlearning/backend/tests/test_basic_words.py)、[test_muted_words.py](/Users/hang/Desktop/app_englishlearning/backend/tests/test_muted_words.py) | 学习控制过滤 | 验证屈折跟随规范单位，独立名词/派生不连带屏蔽 |
| [backend/tests/test_admin.py](/Users/hang/Desktop/app_englishlearning/backend/tests/test_admin.py) | 管理员与持久修正 | 验证别名/重导出不破坏修正、反馈和 ID |
| [README.md](/Users/hang/Desktop/app_englishlearning/README.md) | 架构、导出、部署说明 | 记录新身份和格式、迁移/验证步骤 |

api.ts 的现有请求封装无需为形态功能重写；srs.py 的计数与遗忘处理接口首版保持。ing_usage.py 仍只生成当前固定句的用法提示，不赋予归并判定权。传统中文补充解析器不是首版形态来源，避免扩大其职责。

# 11. 具体函数级修改

## 11.1 修改或拆分现有函数

| 现有函数 / 位置 | 具体修改 |
|---|---|
| word_forms.pos_family | 改为明确标签映射，修 adverb；保留未知 POS |
| word_forms.regular_form_types | 定位为已知形式的标签/候选生成器，不能决定词汇身份 |
| word_forms.dictionary_forms | 支持 v1/v2，返回兼容前向投影；另缓存 v2 的证据索引 |
| word_forms.answer_form_label | 优先使用精确 unit/POS/sense 作用域；未知时保守提示，不拿 heuristic 推动 merge |
| dictionary_senses.allowed_forms / example | 例句匹配仅用属于当前区块/POS 的形式；IRREGULAR 表仅为候选，保留验证 |
| dictionary_senses.inflections_from_root | 拆出明确 infg 解析与 alias 候选，不把所有 aliases 分配给所有 POS |
| dictionary_senses.parse_record | 保留原始 source/entry/block/POS；返回 learning senses 与 morphology 分量，继续阻止子词义混入主词 |
| build_vocab_bundle.read_mac_dictionary | 读取包 manifest，保留原记录身份；语法别名可记录为映射，但不克隆原主词 senses |
| build_vocab_bundle.aligned_senses | 保留已验证对齐；给输出建立 unit binding；不要扩大为语义近似自动对齐 |
| build_vocab_bundle.build | 分类、生成有限相关单位、来源归属与配套文件一致性；输出详细审核清单 |
| export_word_forms.write_word_forms / main | v2 evidence 和 unit/form/relation 输出，兼容投影；避免只按 catalog 词头丢失必要别名条目 |
| database.seed_vocabulary_catalog | 拆为读取验证、词义保存、形态保存、归并计划应用、指纹提交；形态文件变化可单独导入 |
| database.get_vocabulary_lists | 基于规范学习身份统计，保留源词表分母 |
| senses.save_senses / validate_senses | 校验和保存 unit binding、作用域；保留原 sense_key/example ID、修正和归档 |
| senses.migrate_legacy_progress | 保留原“旧句精确唯一对应”原则；不要把形态相似当作义项对应 |
| senses.example_for_sense | 首版保留固定例句；验证规范关系和有效例句 |
| senses.senses_for_word | 保持旧 flat 输出兼容；新详情按 unit、canonical status 组织 |
| sense_learning._DUE_SENSES_SQL / selected_filter / learning_metrics / next_sense_card | 同一规范义项只选一次；新词按单位 membership；已学到期仍不受取消词库选择影响 |
| sense_learning.make_card / record_sense_review | 可选 lexical_unit_id；保持 target_form；保存 presented_form；拒绝归并前的旧活动轮次 |
| sense_learning.record_related_exposure | 拆出“指定规范义项集合曝光”，原函数作为同词头兼容 wrapper |
| adaptive_memory.initial_card / advance_memory / maybe_calibrate | 历史与曝光包含已确认别名；规范 id 去重；模型门槛保持 |
| study_tools.word_meanings / attempt_content | 返回三类 metadata、允许已验证关联详情；先写精确 exposure |
| main.dictionary_lookup | 查询独立身份 + 已验证词形身份，返回多个 entries；不执行整拼写唯一重定向 |
| admin.correct_report_content | 更新后验证原词形证明是否仍适用，无法确认则降为审核；保留旧反馈快照 |
| frontend StudyTools.loadMeanings / App.markAnswerExposed | 使用增强响应和 exposed_sense_ids，不再只用 word id 推断泄露范围 |

## 11.2 建议新增函数，放入现有文件

**dictionary_senses.py：**

- normalize_source_pos(raw)：准确保存和规范化原词性。
- source_entry_identity(root, dictionary_manifest)：读取条目/homograph 身份。
- extract_inflection_references(root, source)：识别受控语法引用和目标 block。
- extract_derivative_entries(root)：只解析显式 derivatives 区块。
- split_entry_identities(parsed)：把纯语法身份与独立意义分开。
- morphology_evidence(...)：形成可校验、可追踪的证明记录。

**word_forms.py：**

- normalize_spelling(value)：统一 trim/casefold/弯引号，保留展示拼写；不去除连字符或强行合并大小写同形异义。
- load_morphology_bundle(path)：版本校验、来源和 catalog 指纹检查。
- reverse_form_index(bundle)：surface → 多个 unit/form 候选。
- classify_lexical_entry(surface, entry, context)：五类分类和 action。
- resolve_lexical_entries(conn, surface, user_id, pos=None)：保留多身份、权限和 UNKNOWN。
- verified_relation_graph(conn, unit_ids, user_id)：只返回实际可见、验证过的一层关系。
- morphology_for_unit(conn, unit_id, user_id)：分别返回 inflections / derived / related。

**database.py / senses.py：**

- validate_morphology_bindings(...)、save_lexical_units(...)、save_word_form_links(...)、save_lexical_relations(...)。
- backfill_legacy_lexical_units(conn)：只增加 legacy 分组。
- canonical_sense_id(conn, sense_id, user_id)：验证无环及权限。
- equivalent_sense_ids(conn, canonical_id, user_id)：供历史查询；只读取已审核别名。
- plan_inflection_consolidation(conn, mappings)：只生成计划。
- apply_safe_consolidation(conn, plan)：严格检查指纹及前置条件，处理安全无冲突情形。
- save_unit_memberships(...)：保留来源拼写、原位置和规范单位归属。

**sense_learning.py / study_tools.py：**

- record_sense_exposures(conn, user_id, canonical_ids, source_attempt_id=None)：泛化精确曝光。
- lexical_details_for_attempt(...)：验证所有权、关联范围，记录曝光后返回详情。

这些新增函数无需新建庞大的 NLP 模块；导出代码依赖 lxml，服务端身份解析仅依赖已部署 JSON/SQLite。事务由调用者负责，避免函数内重复 BEGIN。

# 12. API / type / schema 修改

## 12.1 导出格式

建议 word_forms.json v2 保留原 words 字段作为兼容提示投影，并增加：

~~~json
{
  "format_version": 2,
  "resolver_version": "morphology-v1",
  "source": "macOS Dictionary",
  "catalog_sha256": "<paired catalog fingerprint>",
  "dictionaries": [
    {
      "id": "com.apple.dictionary.NOAD",
      "version": "2.6",
      "body_sha256": "<actual source fingerprint>"
    }
  ],
  "words": "<existing v1 forward projection>",
  "lexical_units": "<stable unit keys with POS and source references>",
  "sense_bindings": "<existing word + sense_key -> unit_key>",
  "forms": "<scoped form links with verification and evidence>",
  "relations": "<typed unit edges with verification and evidence>"
}
~~~

尖括号内容表示字段结构说明，不是准备投入运行的 JSON 实例。

每条证据分清 dictionary_inflection、dictionary_grammar_reference、dictionary_derivative、dictionary_alias_plus_rule、reviewed_override、rule_only。不能将不同证据都抹成 macOS Dictionary。

建议**规划新增** backend/scripts/morphology_overrides.json：保存首版确认的形式与派生关系、源 unit/sense key、expected POS/definition、来源指纹及审核说明。这是新增数据资源，不是当前已存在的文件；不要塞进现有中文义项对齐 overrides 里。可沿用 chinese_gloss_supplements 的“预期定义变化就要求复核”模式。

vocabulary_catalog.json 首版保持 v2；元数据 sidecar 与现有 sense_key 绑定。若以后改变可学词条结构，应另升格式版本并给旧格式适配器，不静默改含义。

## 12.2 查询兼容

现有 dictionary 响应的 word/source/available/definition 保留，增加 entries：

~~~typescript
type LexicalClassification =
  "INFLECTION" | "DERIVED" | "LEXICALIZED_FORM" | "INDEPENDENT" | "UNKNOWN";

type LexicalEntry = {
  lexical_unit_id: number;
  word_id: number | null;
  headword: string;
  pos_group: string;
  classification: LexicalClassification;
  matched_form?: { spelling: string; form_types: string[] };
  learnable: boolean;
  senses: Sense[];
};

type LexicalMetadata = {
  inflected_forms: {
    spelling: string;
    form_types: string[];
    scope_sense_id?: number | null;
  }[];
  derived_words: {
    lexical_unit_id: number;
    headword: string;
    pos_group: string;
    relation_type: string;
    learnable: boolean;
  }[];
  related_words: {
    lexical_unit_id: number;
    headword: string;
    pos_group: string;
    relation_type: string;
    learnable: boolean;
  }[];
};
~~~

- saying 查询可返回 say[VERB] 的 matched_form 和 saying[NOUN]。
- legacy definition 优先维持原来直接词头查询结果；没有直接结果且仅有唯一可学规范目标时可兼容返回该目标。
- 多候选在 entries 明确表达，不把 LIMIT 1 当作词汇判定。
- available 延续“存在可学习内容”的含义；只有关系元数据时 learnable=false，不伪造可学 definition。
- 只返回公共和当前用户可见内容。

## 12.3 学习详情与出题

扩展 POST /api/study/meanings：

~~~typescript
type StudyDetailsInput = {
  attempt_id: string;
  target_lexical_unit_id?: number;
};

type StudyDetailsResult = {
  word: string;                // 旧字段保留
  senses: Sense[];             // 旧字段保留
  entries: LexicalEntry[];
  morphology: LexicalMetadata;
  exposed_sense_ids: number[];
};
~~~

target unit 必须属于当前详情允许的已验证关系且对用户可见；先记录当前题和实际显示目标的曝光。

Card / ReviewResult 可选增加 lexical_unit_id，保留 id、sense_id、example_id、attempt_id、word、answer_form 和 answer_form_label。新字段不要求旧客户端立即升级。

ReviewInput 首版不需要让客户端上报 lemma/form 判定。服务端根据真实已签发题目、例句和规范关系判断，不能信任用户传来的 canonical ID 绕过题目绑定。

数据库 vocabulary_catalog_state 增加 morphology_fingerprint；现有 fingerprint 只涵盖 catalog 和 seed，不包含 word_forms。当前 lru_cache 依赖进程启动，不会自动感知文件原地更新；部署必须成套替换并重启，或实施明确 cache invalidation。

# 13. 测试

## 13.1 分类与词形 unit tests

下表是**新功能验收用例**；“当前”列仅描述已核实的数据，不表示所有新期望已经通过。

| 用例 | 当前数据证据 | 新功能必须满足 |
|---|---|---|
| say → says | word_forms verb third_person_singular；例句有 says | INFLECTION，同一个 say 动词义项不新增状态 |
| say → said | past + past_participle；源还有 said[ADJ] | 动词 INFLECTION；形容词身份独立保留 |
| say → saying[VERB] | ing；say 义项例句有 saying | INFLECTION；不能影响 saying[NOUN] |
| saying[NOUN] | 源有独立名词定义/例句，当前未入 catalog | LEXICALIZED_FORM 或独立身份，保持自己的 sense 与 SRS |
| walk → walked | past + past_participle | INFLECTION |
| walk → walking[VERB] | ing；另有 walking[ADJ] | 动词链接；“活的”形容词不合并 |
| book → books[NOUN] | plural；也有 book 动词的 books | 名词语境链接 book[NOUN]；未给 POS 时允许多候选 |
| small → smaller / smallest | comparative / superlative | 链接 small[ADJ]；不继承错误 verb 标签 |
| absolute → absolutely | 两边独立；源未给该显式派生边 | 审核映射后 DERIVED，两份独立学习 |
| real → really | 两边独立、多 POS | 审核关系后 DERIVED，不能全词头跨 POS 合并 |
| actual → actually | 两边独立 | DERIVED 或未确认关系；始终独立调度 |
| hard → hardly | 两边独立，明显不同定义 | 无屈折合并，不共享成熟或正向家族先验 |
| late → lately | 两边独立，lately=近来 | 无屈折合并 |
| near → nearly | 两边独立 | 无屈折合并，不能凭 -ly 自动断言完整关系 |
| good → better → best | good forward 形式；better/best 有多 POS，well 也映射它们 | 比较/最高级形式识别；其独立名词/动词、good/well 歧义仍保留 |
| go → went / gone[VERB] | went past，gone participle；gone 有独立其他 POS | 语法身份链接；独立形容词/介词不删除 |
| mouse → mice | noun plural；原表还有 mouses | 不规则复数链接，保留变体作用域 |
| child → children | noun plural | 不规则链接 |

再加：

- 英文 adverb、verb、copular verb、transitive verb、plural noun 规范化。
- 同拼写多个原条目、相同 POS 不同来源 homograph。
- read 的 base/past 拼写相同，多语法标签不能丢失。
- found[VERB “建立”] 不能因为它是 find 的过去式拼写而合并到 find。
- lie 的同拼写不同含义与变化路径。
- 不存在的词、只有后缀候选、目标不在词库、没有例句 → UNKNOWN/metadata。
- 普通参见或词源链接不能变成 inflection。
- v1 词形记录只提供提示/候选，不能绕过新的证据要求。
- 来源定义/POS/版本变化时旧 override 失效或进入复核。

## 13.2 Integration tests

- 同一规范义项的各形式对应一个状态、一个 FSRS card。
- saying 名词、walking 形容词、absolutely 的调度独立。
- 期待 said 的句子提交 say 仍错误；错误后纠正不增加独立确认。
- 多身份查询、权限隔离、私有/公共同词头、旧 API 字段兼容。
- MORE meanings 展示过的派生目标曝光记录有效；未展示词不被误标。
- 曝光后刷新、跨设备不能恢复熟词先验。
- 两设备只能完成同一个 round 一次；旧归并轮次返回 409。
- 新词按单位 membership 去重，到期复习延续现有跨词库行为。
- 静音/基础 600 的别名适配与独立名词/派生保留。

## 13.3 Migration tests

- v2、v5、v7、v12、v13 到下一版本，包括无法映射的旧进度。
- Phase 1 原 review_history、SRS、adaptive_memory、队列、固定例句、exposure、修正内容逐行不变。
- 已验证、无学习记录的重复项归并只影响身份/入队。
- 单边历史安全转移保留 ease、interval、Mature、时间与真实队列；无匹配固定句则不转移。
- 双边历史、同日重复、近期失败、Mature 冲突、未知提示历史保持原状并输出计划。
- 失败事务回滚、重复执行幂等、来源指纹不匹配拒绝应用。
- backup 包含 WAL 数据；foreign_key_check 与 integrity_check 通过。
- catalog 重导入不取消审核映射、不复活已归档源内容、不覆盖管理员修正。

## 13.4 Regression tests

继续运行当前的 test_senses、test_adaptive_memory、test_word_forms、test_dictionary_export、test_study_tools、test_basic_words、test_muted_words、test_admin，并补充内容/缓存相关回归。

前端 TypeScript/Vite build；iOS 实机验证弹窗、小屏滚动、键盘、返回、刷新、上一题、朗读与未完成轮次。软件通过不等于已验证 iOS 实机体验。

测试始终使用明确临时 DATA_DIR。完成针对性测试后再做必要的集成验收，不以无关大规模测试代替词汇身份正确性。

# 14. 风险

| 风险 | 具体表现 | 控制措施 |
|---|---|---|
| 错误 POS | adverb 被归 verb | 在任何自动归并前修复并重建证据 |
| 错误 lemma | better 对应 good/well；found 多身份 | 目标 entry/block/POS、上下文及独立义项共同验证 |
| 错误 merge | saying 名词或 said 形容词被整词删除 | 每个 lexical slice 单独判断；不设 whole-word redirect |
| 多 sense | 同 POS 两个意义被当作相同 | 词形链接不等于 sense 合并；没有明确对应不迁移 |
| 不完整字典 | 派生关系、-ing 标签或例句缺失 | 元数据与可学内容分开，保留 UNKNOWN |
| 源变更 | id/定义/HTML 版本改变 | 包 manifest、指纹、expected definition/POS、来源变更审核 |
| 导出文件不一致 | 新 catalog 配旧形式表 | 配套校验和、导入前整体校验、原子替换 |
| 旧历史与成熟 | 合并后虚增确认或覆盖成熟状态 | 原事件不改；状态快照；冲突不自动合并 |
| 例句归属 | 固定句指向另一个 sense | 严格映射；不放宽评分 pair 校验 |
| 曝光污染 | 看词族后首次答对被计为熟词 | 服务端按实际显示集合记录 exposure |
| 数据权限 | 私有词族泄漏给其他用户 | 所有 unit/edge/query 遵守 owner 可见性 |
| 学习控制遗漏 | 别名绕过静音/basic600 | 按规范单位适配并单独保留独立身份 |
| 词库范围变化 | 添加说法名词或关系时自动扩展所有学习范围 | 明确 membership；关系本身不入队 |
| 规则标签过强 | ing/ly/er 被当作语义证明 | 规则产候选；只明确字典/审核证据允许 merge |
| UI 膨胀 | 详情添加常驻说明小字 | 只展示有数据的必要分类，沿用现有弹窗 |

第一版没有必要接入外部 NLP 服务、自动翻译或模型推理来填补关系。若将来选择新数据源，应独立检查真实响应、字段覆盖和现有源 id 对应，不能把新来源的能力当作本项目现状。

# 15. 分阶段实施计划

## Phase 1：基础结构与证据可靠性

交付：

- 修复 POS 子串错误，补回归测试。
- 新增 lexical_units、word_forms、lexical_relations 和 sense unit binding。
- 老数据只增加 legacy 分组，学习状态与例句不动。
- v2 形态导出带来源、区块、验证状态；v1 保留兼容。
- 反查和多身份 API；UNKNOWN/候选报告。
- 首版人工审核少量明确形式及 absolute → absolutely 关系。
- 更多词义最小关系展示和正确 exposure。

验收：原历史逐行不变；saying 名词不会因词尾被合并；派生词状态独立；无应用级全词头重定向。

## Phase 2：受证据约束的 inflection 归并

交付：

- 从明确 infg/grammar-reference 或已核对映射关联语法形式。
- 新屈折来源复用规范单位，不复制义项与曲线。
- 规范单位 membership、canonical-sense 过滤、统计、静音/basic600适配。
- 自动处理无历史重复项；单边历史仅满足全部前提时迁移。
- present-form 事件快照；保留固定例句。
- 双边历史生成待处理计划，不批量自动合并。

验收：say 同一动词义项的 says/said/saying 不新增重复学习状态，其他 say 义项保持独立；irregular 测试和多身份测试通过。

## Phase 3：derivation / word family 覆盖

交付：

- 扩充显式 derivatives 抽取与人工核对关系。
- real/really、actual/actually、eventual/eventually、probable/probably 等关系逐条确认。
- 同族独立学习，关系点击与曝光正确。
- hard/hardly、late/lately 等语义变化关系不传递成熟或熟词确认。
- 不建立词族统一 SRS。

验收：派生关系覆盖提升，复习独立性和首次答题规则无变化。

## Phase 4：词汇化、多身份与历史冲突完善

交付：

- 同 POS 不同源 homograph、sense-specific forms、更完整的 lexicalized-form 判定。
- 无例句元数据与可学内容覆盖继续分开。
- 专门的历史归并计划/receipt/撤销工具，处理经审核的双边进度。
- 根据实际需求独立评估词形识别汇总和可选例句轮换。

验收：复杂历史合并可审计、可回滚；不牺牲现有固定句和复习可靠性。

**词汇化保护必须在 Phase 1/2 就上线。Phase 4 完善覆盖，不意味着之前先把 -ing 名词误合并再修。**

## 明确的第一版实施建议

**可以现在可靠实现：**

- 保留现有 sense SRS，建立三类身份表和双向词形查询。
- 修复 POS，重新导出有区块与来源证据的形式。
- 用明确语法引用或人工核对小集合处理 say/says/said/saying 及常见不规则形式。
- 保留、补入 saying[NOUN] 的实际词典义项/例句，维护独立状态。
- 让 absolute 和 absolutely 分别学习，并通过 reviewed_override 建立词族关系。
- 在已有更多词义弹窗区分屈折、派生、其他关联。
- 数据备份、legacy 分组、无历史重复项保守处理和完整回归。

**需要 heuristic 但只应产候选：**

- 没有明确区块的规则 -s/-ed/-ing/-er/-est。
- 未给显式父子关系的 -ly 派生。
- 当前 ing_usage 的句法细分提示。
- 按同拼写/POS/解释相似度推断源身份。

**受当前字典/导出限制：**

- 任意输入的唯一 lemma。
- 完整 word family、root、affix 分解。
- 完整派生词语用/搭配与所有 lexicalized senses。
- 每个词形的完整发音和高质量配对例句。
- 旧扁平私有词自动对应正确 sense。

**暂不建议：**

- 全库按 suffix 自动合并。
- 把词族熟悉度直接转成 Mature。
- 自动合并双边已有复习历史。
- 为每个词形新增另一套主遗忘曲线。
- 默认随机替换用户的固定例句。
- 在这次功能中顺手恢复用户导入。

第一版应是 **Phase 1 + Phase 2 的小范围、经过核对的自动化**：在不变更原学习状态的前提下先建立身份，再让明确的动词形式复用规范义项；对历史冲突保留原状；对独立名词和派生词保持独立学习。未知分类是可接受结果，错误归并不是。
