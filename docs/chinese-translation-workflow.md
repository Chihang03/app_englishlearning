# 对话模型翻译接口

`backend/scripts/translation_exchange.py` 提供离线导出、校验、导入及生成词库的接口。Codex、其他对话模型或未来的 API 适配器都可以使用相同的文件协议。工具本身不调用模型，不需要 API 密钥，也不写应用数据库。

## 节省 token 的流程

1. 先复用已有字典和中文补充；只导出仍然缺失的义项。同一个单词、词性、英文释义完全相同的记录只翻译一次。
2. 先用 20 个义项检查翻译质量，再按每批 100 个处理；质量稳定后可以试 200 个。每批独立运行，避免反复携带这个项目的代码和长对话历史。
3. 首轮只发编号、单词、词性和英文释义。义项键、来源、数据库编号留在本地 manifest 中。首轮不发例句；不确定项返回 `null`，重试时只给这些项增加一条例句。
4. 只返回编号和简短中文释义，不重述英文，不解释翻译过程。已导入的结果作为持久缓存，下次导出会跳过。
5. 校验失败就修复对应批次，不重做整份词库。导入操作顺序执行。

这减少输入和输出的 token；批次越大并不一定越好，错译和返工也会增加消耗。模型价格、推理用量和实际 token 数需要分别比较。`--ephemeral` 只控制会话文件是否保存，不会自动降低 token。

## 1. 导出

在项目根目录运行，输出必须是新目录：

```sh
python3 backend/scripts/translation_exchange.py export \
  --output /tmp/english-cn-pilot --batch-size 20 --limit 20
```

每批生成三个文件：

- `batch-0001.input.jsonl`：发送给模型的精简输入。
- `batch-0001.schema.json`：可选的结构化输出约束。
- `batch-0001.manifest.json`：留在本地的义项映射与原文快照，不发送给模型。

`prompt.txt` 是固定指令。输入示例：

```jsonl
{"batch":"f9b980281b118ea7"}
[1,"bank","名词","the land alongside a river"]
[2,"bank","动词","to deposit money"]
```

对应结果为一个 JSON 对象，保存为 `batch-0001.result.json`：

```json
{"batch":"f9b980281b118ea7","items":[[1,"河岸"],[2,null]]}
```

`batch` 必须复制实际输入中的值，每个编号恰好返回一次。这个协议也可以由未来的对话 API 接收和返回；模型名称、密钥及网络调用不耦合进词库处理代码。

## 2. 使用 Codex CLI（需要开始翻译时再运行）

CLI 复用本机已登录的认证。当前机器支持以下选项。使用独立批次目录，输入通过标准输入传入，最终结果单独保存：

```sh
cd /tmp/english-cn-pilot
cat prompt.txt batch-0001.input.jsonl | codex exec - \
  --skip-git-repo-check --sandbox read-only --ephemeral \
  --output-schema batch-0001.schema.json \
  --output-last-message batch-0001.result.json
```

保留当前配置的模型，先检查小批次质量。也可以把相同输入粘贴到一个新对话中，将纯 JSON 回答保存成结果文件。schema 能约束结构，语义仍需要抽查。

`--json` 输出的是 CLI 事件日志，不是上述翻译结果。需要观察用量时可单独保存事件日志，仍用 `--output-last-message` 获取翻译结果。Codex 固定系统指令和个人配置也可能进入上下文，因此这里不承诺具体节省比例。

参考：[Codex 非交互运行](https://learn.chatgpt.com/docs/non-interactive-mode)、[CLI 命令选项](https://learn.chatgpt.com/docs/developer-commands?surface=cli)。

## 3. 校验并导入

回到项目根目录：

```sh
python3 backend/scripts/translation_exchange.py import \
  --manifest /tmp/english-cn-pilot/batch-0001.manifest.json \
  --result /tmp/english-cn-pilot/batch-0001.result.json --dry-run

python3 backend/scripts/translation_exchange.py import \
  --manifest /tmp/english-cn-pilot/batch-0001.manifest.json \
  --result /tmp/english-cn-pilot/batch-0001.result.json
```

导入检查批次、重复或缺失编号、中文内容、原始英文和词性是否发生变化，拒绝覆盖不同的已有中文。通过后合并到 `chinese_gloss_supplements.json`，并保存旧文件备份；重复导入相同结果不会重复增加条目。`null` 保留为待译项。`validate` 子命令只校验原文和结果，`import --dry-run` 还会检查与现有补充的冲突。

自动校验保障格式和义项映射，不保证中文语义正确。补充记录标记为 `model_translation`，不会标记成人工核对。

只重试空结果并附带例句：

```sh
python3 backend/scripts/translation_exchange.py export \
  --output /tmp/english-cn-retry --examples \
  --retry-manifest /tmp/english-cn-pilot/batch-0001.manifest.json \
  --retry-result /tmp/english-cn-pilot/batch-0001.result.json
```

正式批量导出时省略 `--limit`，默认每批 100 个：

```sh
python3 backend/scripts/translation_exchange.py export --output /tmp/english-cn-full
```

## 4. 生成可部署的词库

导入只保存补充文件。生成新词库时保留原始文件，便于核对和数据库安全更新：

```sh
python3 backend/scripts/translation_exchange.py materialize \
  --output /tmp/vocabulary_catalog.translated.json
```

确认翻译质量后，将生成的文件作为新 `vocabulary_catalog.json`，保留旧版 catalog。部署中文释义时使用现有 `backend/scripts/apply_chinese_glosses_to_db.py` 的旧、新 catalog 对照和 SQLite 备份流程。这个流程只更新缺失释义，保留学习记录、义项编号及例句。不要直接通过重新导入整个数据库来应用翻译。
