# 证据索引与公开边界

归档日期：2026-10-27

## 公开材料对应关系

| 简历主张 | 仓库证据 | 当前边界 |
|---|---|---|
| 端到端生产管线 | `README.md`、`docs/03_架构设计.md`、`orchestrator.py`、`engine.py`、`packager.py` | 生产依赖外部模型和环境变量 |
| 断点恢复与调用审计 | `docs/01_项目计划.md`、`state_store.py`、`orchestrator.py` | 历史运行数据不随公开代码提交 |
| 流式请求修复 | `production-evidence.md`、`engine.py`、`native_check.py` | 约 125 秒是历史样本，不是当前 SLA |
| 章节控制层 | `control_compiler.py`、`control_context.py`、`control_contracts.py`、`control_runtime.py`、`chapter_contract.py` | 新增控制层的真实全流程验证仍有缺口 |
| 事实与质量门禁 | `story_bible.py`、`story_bible_checks.py`、`quality_checks.py`、`quality_gate.py` | 确定性检查和编辑判断分开；不等于无问题 |
| 锚定修复与问题账本 | `repair_queue.py`、`repair_pipeline.py`、`repair_validator.py`、`final_editor_*.py` | 修复基础设施已测，完整作品质量改进未被单次重跑证明 |
| 12 章样稿 | `production-evidence.md`、本机 `shorts/` 归档 | 正文和运行产物不进入公开 GitHub |
| 375 项回归 | 当前命令 `py -3 -m unittest discover -p "test_*.py"` | 测试通过不证明小说可发布；输出有已知 ResourceWarning |

## 当前可公开的内容

- Python 源码、测试、非密钥运行配置、架构文档、运维文档和本目录下的项目材料。
- 不含真实凭据的模型路由和质量控制设计。
- 可复核的测试命令、结果口径和限制说明。

## 明确排除的内容

- `.env`、`keys.local.json`、任何 API key、token、证书和私钥。
- `state.db`、`.codex-runtime/`、运行日志、临时目录和浏览器/桌面桥接状态。
- `shorts/`、`out/`、`probe/` 中的完整小说正文、EPUB、发布包和模型原始响应。
- 只服务于本机实验的缓存、依赖目录和生成临时脚本。

## 结果解释规则

1. 测试通过只证明代码路径满足测试断言，不证明生成文本达到编辑或出版标准。
2. `full.md`、EPUB、manifest 或质量分数只证明产物存在或某个检查路径通过，不自动升级为 `release_ready`。
3. 生成模型具有随机性；单次重跑的 findings 变化不能直接归因于代码改进。
4. 任何“完成”“稳定”“质量提升”都必须带样本、版本、命令和限制，否则只能写成设计目标。

## 建议的 GitHub 展示顺序

1. 根目录 `README.md`：项目定位、快速开始和公开边界。
2. `docs/portfolio/project-dossier.md`：系统设计和量化结果。
3. `docs/portfolio/resume-bullets.md`：简历和面试表述。
4. `docs/portfolio/evidence-index.md`：证据与限制。
5. `docs/03_架构设计.md`、`docs/05_运维手册.md`、测试文件：深入阅读和复核入口。
