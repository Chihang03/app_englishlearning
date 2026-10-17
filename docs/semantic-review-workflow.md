# 词义语义核查与双语标准化

词典仍以 `vocabulary_catalog.json`、中文释义/句子补充文件及数据库中的源义项为依据。新增核查复用 `translation_exchange.run_batches` 和现有 Codex 登录，使用 `gpt-6-luna` / `low`，不新增 API 服务、密钥或前端调用。本机 Luna 调用通道支持的最低推理档位为 `low`，使用该实际支持档位。

## 处理和缓存

当前推荐使用 `semantic_staged_exchange.py`。完整词表先做覆盖审计；已完成且源指纹相同的词复用缓存，其余词使用 Luna low 分阶段核查。一次语义请求理解同词的全部源上下文，仅输出核心义项、词性、需要纠正的旧译文编号及具体疑点；翻译请求随后仅处理缺译或已被指出错误的句子，不让模型重复生成原句或已有准确中文。每批按输入长度限制，复杂词单独处理，义项编号为 `S1`、例句编号为 `E1`，输出 schema 逐词限制允许引用的编号，防止两类编号混用。

模型回复可局部接收：同核心/词性与相同内容的重复编号自动去重；冲突组、未知编号和无法确定的义项保留为待处理，不会丢弃其他可靠组。补做只请求缺失/有疑点的义项，并提供已确认核心作为参考；缺少或冲突的译文只补做对应句子。英文定义由程序从该组的原始成员中选择，避免模型误指向其他组。每次请求的 manifest、输入、schema、原始结果、调用记录、接收凭据及逐词 checkpoint 均保留；重跑复用完成结果，调用失败后停在本轮，恢复后补做失败请求。不会因一次超时清空已保存的分组或译文。

恢复时，已经接收但仍有漏项的响应不会把漏项永久缓存为完成；相同轮数重跑也会为尚未完成的词/句追加任务，原始响应不被替换。分组任务同时绑定规划时的检查点；恢复时旧检查点任务不覆盖新状态，并为当前状态重新规划。分组发生变化后，旧任务的核心中文/词性与当前分组不一致时，旧翻译或审计结果不会写入新分组。检查点或接收凭据写入失败会阻止后续阶段继续使用不完整状态。请求前检查 schema 的最小/最大项目数、长度等边界，直接拒绝矛盾约束，避免无效模型消耗。

证据中显式引用的 `E` 编号必须属于本组，外组证据仅暂缓相关组。模型若在纠错理由中明确说明“无需更正”或“无语义错误”，程序保留原译，不触发改写。真正提出的旧译文改写另经一次 Luna low 独立比较原译与候选，同时读取原中英文定义和同义项例句语境：原译已准确时保留原文，原译确有实质错误且候选可靠才接受，二者都不可靠则只暂缓相关核心并补做该句。复核发现核心归组与句境不符时，将相关组重新排入语义补做队列；不把惯用义直接按字面义改译。缺译补全无需执行这次旧译文对比。`pending-report.json` 保留各词未完成的具体义项、语义疑点、完整性问题和历史核心保护原因，便于复盘。

最终提案区分 `ready`、`partial`、`manual`。`partial` 必须列出与未应用源义项完全对应的 `pending_keys`；每个已应用成员仍须完整保留该原义项的所有例句，并有对应中文。同一候选核心中，只有某个源成员缺译或未通过校验时，可先应用其他完整成员，缺项单独补做后再加入核心；不会为凑完整而丢弃该成员的原句。候选分组和已完成译文保存在 checkpoint。已有历史核心如果会被新组拆分，只暂缓相关组。原始义项、例句及来源始终保留，未应用部分原样延续；不把缺失内容当成已完成。没有源义项或例句的词单独列入覆盖报告，不冒充核查通过。

部分提案应用后可继续补齐：当前数据库必须恰好匹配上一次部分提案的输出指纹，来源须为同一原始快照；新的提案不能减少已接受的义项，也不能拆分历史核心。人工修改仍会使旧指纹失效。每次应用保存完整来源和结果收据，沿用现有学习进度合并机制，无需新增数据库表或前端提示。

缺少配套例句而未进入主目录的词可通过 `source_word_completions.json` 增量恢复。该文件绑定原目录的内容指纹，保存原典证据、原释义/词性/键和明确标记来源的双语例句；AI 生成的例句标为 `Luna AI-generated example`，不会冒充词典原句。没有英文定义时保留为空。启动只为没有活跃源义项的公共词补源，已有内容和私人词不会被覆盖；课程成员取原始课程词表，重复启动不重复创建。错误条目独立回滚，目录指纹不符则跳过整份补源。

`semantic_exchange.py` 从现有数据库导出整个单词。源词头、所有活跃义项、词性、中英文定义、学习资格、全部原句/译文/目标词形和来源参与 SHA-256 指纹。确定性格式检查列出缺失中文、缺失译文和重复项；只有词性、中英文定义和全部例句/译文完全相同的条目可直接去重。中文相同但上下文不同仍交给 Luna。

模型输入使用临时义项/例句编号，模型只输出分组、核心中文、英文定义来源编号和需要补全或纠正的中文译文。已有准确译文可以省略，程序保留原文；替换已有中文需要 `reason` 说明原译文的具体语义错误，避免仅换同义表述。缺译例句必须返回。英文原句和源 key 不需要重复生成，从源映射恢复，降低生成量并防止原句漂移。每个单词全部义项在同一个请求中处理；默认每批 5 个词，可用 `--batch-size 1` 隔离复杂单词。模型输出由 Pydantic 严格结构、完整义项编号覆盖、译文编号与缺译覆盖、中文内容、原句目标词形、定义来源及重复核心等校验共同检查。Luna 已指定为相同词性和相同核心释义的输出组会确定性归并，原义项的中文相似度不会触发此规则。词性不能明显违背例句（如 highly 修饰的用法不能标成名词）。这些程序检查不能代替语义判断；真正的合并、翻译和语义对应由 Luna 判断。

可靠结果存入 `backend/data/semantic_reviews.json`。不确定的词标为 `manual` 并保留原数据；不会为了填满字段编造定义、例句或译文。源数据未改变的已完成词会被跳过。`manual` 也缓存，只有显式 `--retry-manual` 才重试相同来源。调用失败、超时和非法结构只留下 pending 提案，不成为完成缓存。重新运行同一个目录会跳过合法完成批次、重试未完成批次；超时终止整个 CLI 进程组。无需在打开详情时调用模型。

## 增量执行

分阶段执行完整四级核查：

```sh
PYTHONPATH=backend backend/.venv/bin/python backend/scripts/semantic_staged_exchange.py prepare \
  --db /path/to/vocabulary.db --directory /tmp/cet4-review --list-id cet4
PYTHONPATH=backend backend/.venv/bin/python backend/scripts/semantic_staged_exchange.py run \
  --directory /tmp/cet4-review --workers 3 --rounds 3 --timeout 360
PYTHONPATH=backend backend/.venv/bin/python backend/scripts/semantic_staged_exchange.py collect \
  --directory /tmp/cet4-review
PYTHONPATH=backend backend/.venv/bin/python backend/scripts/semantic_exchange.py apply-db \
  --db /path/to/vocabulary.db --dry-run
```

`prepare` 的默认来源为既有核查包中保留的源快照；提供 `--db` 可在只读事务中读取当前数据库的原始源快照及历史核心边界。`--source-snapshot` 可读取相同格式的只含公共词典数据的服务器导出，避免传输用户数据库。三者选定后冻结到工作目录；`run` 可以直接续跑同一目录，增加 `--rounds` 可继续尚未规划的补做轮次。`run-report.json` 区分缓存、完成、部分完成、未完成和源材料缺失，不将全表结构审计计作新增模型调用。`collect` 校验来源基线，保留可靠旧结果且拒绝覆盖核查期间发生的并行修改。应用前仍需生产 dry-run，应用时自动 SQLite 备份。

升级本地校验规则后可执行 `recheck --directory ...`，只重新检查候选 checkpoint 并保留原始模型输出；随后增加轮次补做被局部暂缓的义项。`audit-translations --directory ...` 可单独补跑旧译文改写的独立复核。`summarize --directory ...` 仅汇总已保存结果，不调用模型。

以下旧版单阶段命令继续保留，便于兼容既有任务目录；新全库核查优先使用上述分阶段方式。

先在应用环境下初始化数据库到当前 schema；服务启动会自动完成 v21 迁移并保存升级前备份。以下路径中的数据库可以是备份或实际部署数据库，不要遗漏明确路径。

```sh
PYTHONPATH=backend backend/.venv/bin/python backend/scripts/semantic_exchange.py export \
  --db /path/to/vocabulary.db --output /tmp/semantic-batch --limit 100
PYTHONPATH=backend backend/.venv/bin/python backend/scripts/semantic_exchange.py run \
  --directory /tmp/semantic-batch --timeout 240
PYTHONPATH=backend backend/.venv/bin/python backend/scripts/semantic_exchange.py collect \
  --directory /tmp/semantic-batch
PYTHONPATH=backend backend/.venv/bin/python backend/scripts/semantic_exchange.py apply-db \
  --db /path/to/vocabulary.db --dry-run
PYTHONPATH=backend backend/.venv/bin/python backend/scripts/semantic_exchange.py apply-db \
  --db /path/to/vocabulary.db
```

省略 `--limit` 可导出全部未缓存词；`--words beginning professional` 可定向处理。`run --limit-batches 10` 可限制单次调用量。如果导出报告 `exported_words=0`，无需运行模型。失败后可直接重跑同目录；`collect` 只收集合法完成结果。每批及运行报告保留模型、推理档位、原数据和结构化输出，便于审计。不要把 pending 文件手动改名为完成结果。

应用启动在原词库、补充翻译、词形和课程分组同步结束后重放该 bundle。生产迁移只接受与导出完全一致的源词，或同一已应用输出的指纹；不同服务器的数字 ID 无需一致，因为提案使用源 key 与原句映射。词源发生变化会跳过旧提案并计入 `stale`，应重新导出和核查。管理员覆盖或例句修正也会使源指纹失效。

## 学习记录与原数据

原词典文件保留；SQLite `semantic_review_receipts` 还保存应用前完整词义快照、原词性/词形身份、结果和应用后指纹。原英文定义与例句不修改，补全译文及中文主释义的原值在快照中保留。原 `sense_id`、`example_id`、历史答题内容和时间不删除。`learning_unit_senses` 保存源义项到核心的映射。

合并复用现有 `_merge_group`：单个已学曲线原样延续；多个曲线采取较早到期、较短间隔等保守规则，并在 `learning_unit_merges` 留存每条原曲线。重复执行不会创建新曲线或重复累计答题。旧题回合退休并要求客户端获取新回合，避免在答题中途更换题目。课程范围取被合并源义项原有覆盖的并集，保留明确空范围；启动时不会用旧编辑分组覆盖 AI 核心。

已有核心不允许被提案静默拆分，启用/排除义项也不允许混合。需要拆分的词会保留为 `stale`，需基于历史事件设计显式迁移。原有 `rollback_learning_merge.py` 可以恢复合并快照；存在新答案时会拒绝自动回滚，避免覆盖新学习事件。`apply-db` 用 SQLite backup API 保存生产备份，dry-run 在内存副本执行相同合并与外键检查。

在线详情与离线缓存保留源项，视觉上根据 `learning_unit_id` 展示核心中文、原英文辅助定义、必要语义差异和去重后的双语例句。主释义不再用英文定义兜底。未核查且缺中文的原内容显示“待核查”，原英文仍可在详情中查看。无新增提示小字。

## 验证范围

针对性测试覆盖完全重复与近义合并、独立词义保留、英文混入中文、翻译缺失、错误词性、非法结果、调用失败/超时、缓存/源变化、重复执行、原进度/历史和课程范围。原合并机制另有多个曲线、跨账号隔离和安全回滚测试。

本轮 CET4 的全词表覆盖、部分完成项和原典未配句项见 [四级核查报告](cet4-semantic-audit-2026-10-16.md)。部署 bundle 的记录代表已经处理的词；并不意味着其他课程全词库已经核查。历史库应通过上述增量命令逐批扩展，失败或不确定的内容保留原数据。iPhone 上的展示需与自动化浏览器/后端验证分开报告。
