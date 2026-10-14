# 词义语义核查与双语标准化

词典仍以 `vocabulary_catalog.json`、中文释义/句子补充文件及数据库中的源义项为依据。新增核查复用 `translation_exchange.run_batches` 和现有 Codex 登录，使用 `gpt-6-luna` / `low`，不新增 API 服务、密钥或前端调用。API 文档支持 `none`，但本机 Codex Luna 目录列出的最低档是 `low`，本通道使用该实际支持档位。

## 处理和缓存

`semantic_exchange.py` 从现有数据库导出整个单词。源词头、所有活跃义项、词性、中英文定义、学习资格、全部原句/译文/目标词形和来源参与 SHA-256 指纹。确定性格式检查列出缺失中文、缺失译文和重复项；只有词性、中英文定义和全部例句/译文完全相同的条目可直接去重。中文相同但上下文不同仍交给 Luna。

模型输入使用临时义项/例句编号，模型只输出分组、核心中文、英文定义来源编号和需要补全或纠正的中文译文。已有准确译文可以省略，程序保留原文；替换已有中文需要 `reason` 说明原译文的具体语义错误，避免仅换同义表述。缺译例句必须返回。英文原句和源 key 不需要重复生成，从源映射恢复，降低生成量并防止原句漂移。每个单词全部义项在同一个请求中处理；默认每批 5 个词，可用 `--batch-size 1` 隔离复杂单词。模型输出由 Pydantic 严格结构、完整义项编号覆盖、译文编号与缺译覆盖、中文内容、原句目标词形、定义来源及重复核心等校验共同检查。Luna 已指定为相同词性和相同核心释义的输出组会确定性归并，原义项的中文相似度不会触发此规则。词性不能明显违背例句（如 highly 修饰的用法不能标成名词）。这些程序检查不能代替语义判断；真正的合并、翻译和语义对应由 Luna 判断。

可靠结果存入 `backend/data/semantic_reviews.json`。不确定的词标为 `manual` 并保留原数据；不会为了填满字段编造定义、例句或译文。源数据未改变的已完成词会被跳过。`manual` 也缓存，只有显式 `--retry-manual` 才重试相同来源。调用失败、超时和非法结构只留下 pending 提案，不成为完成缓存。重新运行同一个目录会跳过合法完成批次、重试未完成批次；超时终止整个 CLI 进程组。无需在打开详情时调用模型。

## 增量执行

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

部署 bundle 的记录代表已经处理的词；并不意味着全词库已经核查。历史库应通过上述增量命令逐批扩展，失败或不确定的内容保留原数据。iPhone 上的展示需与自动化浏览器/后端验证分开报告。
