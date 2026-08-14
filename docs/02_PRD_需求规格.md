# PRD：英文短篇全自动生产线

- 文档版本：v3.0（2026-08-14，v1 正式版交付后更新）
- 原则：每条需求附可验收标准；结论以实测为准；英文质量必须可量化

---

## 1. 术语

| 术语 | 定义 |
|---|---|
| 写作引擎 writing-engine | 自研英文短篇产出模块（engine.py，替代 InkOS 生产）|
| 质量门槛 quality gate | 每章盲评 naturalness ≥4，不过带反馈重写 |
| 质检拦截 verify gate | 审稿 native ≥4 / engagement ≥3，不过 exit 2 不包装 |
| 书内词条库 lessons | 每本书独立 lessons.json，失败反馈回灌 |
| 方向 direction | 喂给写作引擎的题材描述文本 |
| 故事 story | 一部 12 章英文短篇 |

---

## 2. 功能需求（按优先级）

### FR-0 英文写作引擎（P0，已完成 ✅）

**描述**：自研英文短篇产出算法，替代 InkOS 生产。核心：大纲、world bible、分章写作（字数硬校验）、风格样本库、AI 味抑制、中文摘要。

**验收标准**（v1 交付后勾选）：
- [x] a. **质量探针**：自研引擎 vs InkOS 量化对比完成（字数达标 100% vs 25%）
- [x] b. **模型选型**：写作模型接入母语级（GPT-5.5 分段续写为主，Gemini/deepseek 备份）
- [x] c. **字数符合度**：单章词数达标率 100%（目标 620-780，硬校验 + 不符重写）
- [x] d. **AI 味抑制**：禁用词表 + 密度校验生效
- [x] e. **native-check**：能识别"词义不地道 / 中式英语 / 句意错误"样本（埋雷 5/5）
- [x] f. **盲评结论**：GPT 分段续写 naturalness 5（盲评误判真人）
- [x] g. **中文对照**：每部产出附中文摘要

### FR-1 英文选品雷达（P1，已完成 ✅）

**描述**：抓取英文榜单，量化赛道空位，LLM 产出题材推荐。

**验收标准**：
- [x] a. RoyalRoad 三榜抓取 ≥10 条，含书名与 ID
- [x] b. **赛道空位量化**：先统计榜单题材分布，LLM 基于统计数字推荐
- [x] c. LLM 推荐 ≥5 条，每条含 genre/concept/confidence/reasoning

### FR-2 方向管理（已完成 ✅）

- [x] 采纳/跳过推荐，生成方向文件 `directions/rec_N.txt`
- [x] 写作引擎支持 `--direction-file`

### FR-3 写作质量门槛（新增，已完成 ✅）

**描述**：每章写后 GPT 盲评 naturalness，<4 带反馈重写，直到达标。

**验收标准**：
- [x] a. 首写 <4 的章自动带反馈重写（实测 ch1 从 2-3 拉到 5）
- [x] b. 字数不足（<620）也重写（修复了"质量过字数不足"的 bug）
- [x] c. 已接入 orchestrator 全流程（`--quality-gate --quality-min 4`）

### FR-4 包装（已完成 ✅）

- [x] publish_manifest.json/md + EPUB（zipfile 手写，16 文件规范）

### FR-5 调度 + 看板（已完成 ✅）

- [x] 完整 cycle 跑通（radar→采纳→写作→质检→包装）
- [x] 看板 localhost:8900：进度卡、推荐列表、产物清单、开关、触发生产
- [x] 质检拦截：native_check exit 2 → orchestrator 不包装
- [x] 限流：日产量上限 + token 预算

### FR-6 书内自学习闭环（新增，已完成 ✅）

**描述**：每本书独立 lessons.json，盲评失败（verdict=ai）沉淀"avoid"词条，后续章节注入。

**验收标准**：
- [x] a. 盲评失败沉淀词条到 `shorts/{story_id}/lessons.json`
- [x] b. 后续章节写作注入本书词条
- [x] c. **隔离**：全局规则包（ai_tells_en.json / profiles.py）只读不写，书与书零交叉

### FR-7 多模型封装（新增，已完成 ✅）

**描述**：profiles.py 按模型名自动路由（backend + prompt 包 + max_tokens）。

**验收标准**：
- [x] a. 写作：thinking 模型用 minimal 包（3.1-pro/GPT），轻量模型用 full 包（flash/deepseek）
- [x] b. 检测：GPT → GLM → Gemini 降级链，自动切换
- [x] c. 模型切换只需改 config.json

---

## 3. 非功能需求

| 项 | 要求 | 状态 |
|---|---|---|
| 报告纪律 | 严禁假数据、严禁夸大结论 | ✅ 写入文档附录 |
| 隔离性 | 书间词条零交叉，全局规则包只读 | ✅ 实测验证 |
| 降级容错 | 单模型挂自动降级 | ✅ GPT→GLM→Gemini |
| 中转容错 | 504/400 自动重试 3 次 | ✅ 已加 |
