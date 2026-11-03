# 项目协作说明

## 项目定位

主要交付运行在 iOS 主屏幕上的 Web App。前端使用 React/TypeScript，生产后端使用 Rust 与 SQLite。项目整体构想和现行规则按下面的文档路由查阅。

## Git 流程

1. 相关代码修改完成后自动提交到 main、推送并连接服务器部署。
2. 提交信息使用中文 Conventional Commits，一次提交围绕一个明确目的。
3. 提交前运行 `python3 scripts/check_sensitive_content.py`。首次配置本地 Git hook 执行 `git config core.hooksPath .githooks`。
4. 提交文档时检查相对链接。文档通过 Git 分发，运行时发布用于实际代码和资源变更。

## GUI 与文案

* 复用整体页面的导航、图标、卡片、按钮和输入组件，优先验证 iOS 触摸、软键盘和小屏布局。
* 界面避免重复说明。Avoid unnecessary secondary labels and descriptions. Let the layout do the explaining. 例如已有“使用通行密钥登录”按钮时，无需另加“首次使用需在设置中添加通行密钥”；已有“不再学习此单词”操作时，无需追加“已消音 所有义项”等小字。

## 文档路由表

| 阅读目的                     | 文档                                                                                           | 涵盖内容                                          |
| ---------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| 理解整个项目                 | [项目构想与产品原则](docs/project/01-项目构想.md)                                               | 用户、目标、学习闭环、已实现范围与验证方向        |
| 理解内容怎样组织             | [词库与内容模型](docs/project/02-词库与内容模型.md)                                             | 词头、义项、学习单元、词形、固定例句、内容生产    |
| 判断一个词为什么出现或被排除 | [学习复习与排除规则](docs/project/03-学习复习与排除规则.md)                                     | 作答、SRS、长期熟记、基础排除、手动排除、水平暂缓 |
| 理解水平检测的职责           | [水平检测与学习起点](docs/project/04-水平检测与学习起点.md)                                     | 32–40 题、短确认、结果应用、旧版本兼容           |
| 修改页面和控件               | [界面与交互](docs/project/05-界面与交互.md)                                                     | 导航、组件复用、输入、反馈、移动端验收            |
| 修改账户、后台和持久化       | [账号管理与数据架构](docs/project/06-账号管理与数据架构.md)                                     | 认证、权限、管理审核、数据关系、事务和缓存        |
| 构建、验证及发布             | [工程部署与验证](docs/project/07-工程部署与验证.md)                                             | 原生架构、测试、部署、回退、验收边界              |
| 修改测验具体参数             | [词汇测验详细说明](docs/vocabulary-assessment.md)                                               | 选题、结束条件、暂缓、校准门槛和接口              |
| 处理词典语义与中文内容       | [语义审核](docs/semantic-review-workflow.md) · [中文翻译](docs/chinese-translation-workflow.md) | 内容校验与人工维护                                |
| 维护词形与词族               | [词形审核与计划](docs/word-morphology-audit-and-plan.md)                                        | 词汇身份、屈折与派生                              |
| 执行生产发布与恢复           | [Rust 发布手册](deploy/RUST.md) · [缓存说明](deploy/CACHE.md)                                   | 运行环境、完整性校验、静态资源和恢复              |

页面与 API 的详细路由查阅[页面与接口路由总表](docs/项目文档路由表.md)。修改对应模块时同步其说明和路由。

## 本地运维配置

真实连接信息保存在仓库外的 `~/.config/app_englishlearning/deploy.env`，运维说明位于同目录 `operations.md`。连接服务器前从该配置读取部署主机、用户和 SSH 密钥路径。配置缺失时向维护者获取，禁止将真实参数写回仓库。

服务器发布脚本读取 `/etc/englishlearning-deploy.env` 中的 `PUBLIC_ORIGIN`，可通过 `ENGLISH_DEPLOY_CONFIG` 指定其他配置路径。通用字段见 `deploy/deploy.env.example`，发布过程见 `deploy/RUST.md`。

私钥、真实环境配置和包含清理前历史的备份只保存在受限访问位置，不提交、不上传到代码仓库，不把内容打印到日志。
