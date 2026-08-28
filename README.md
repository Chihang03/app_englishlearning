# Context Vocabulary Trainer

本地运行的语境单词训练器。系统用英文例句挖空让用户根据上下文和中文释义回忆单词，并用 SQLite 持久化保存学习记录与 SRS 状态。

## 技术栈

- Frontend: React, TypeScript, TailwindCSS, Vite
- Backend: Python FastAPI
- Database: SQLite
- TTS: macOS `/usr/bin/say`

## 目录

```text
backend/
  app/
    main.py          # FastAPI API
    database.py      # SQLite schema and queries
    srs.py           # SRS scheduling
    tts.py           # macOS say wrapper
  data/
    seed_words.json  # 初始示例词库
frontend/
  src/
    App.tsx
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

## 数据

首次启动后端时会自动创建 `backend/data/vocabulary.db`，并导入 `backend/data/seed_words.json` 中的示例单词。后续学习记录、SRS 状态和设置都会写入同一个 SQLite 数据库。

## API 摘要

- `GET /api/stats` 首页统计
- `GET /api/next` 获取下一题
- `POST /api/review` 提交答案并更新 SRS
- `POST /api/tts` 调用 macOS TTS 朗读
- `GET /api/settings` 获取发音设置和可用英文语音
- `PATCH /api/settings` 更新语音和语速
- `GET /api/dictionary/{word}` 尝试通过 macOS DictionaryServices 查询系统词典
- `POST /api/words` 新增单词
- `POST /api/words/import` 批量导入单词

## macOS Dictionary

后端提供 `GET /api/dictionary/{word}`，优先尝试调用 macOS 的 `DictionaryServices` 本地框架。该框架是否能返回内容取决于当前系统已安装并启用的词典；如果系统不返回词条，接口会返回 `available: false`。学习用词条仍可通过 `seed_words.json`、`POST /api/words` 或 `POST /api/words/import` 导入，确保应用不依赖网络。
