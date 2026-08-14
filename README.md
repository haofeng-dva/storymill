# 英文短篇全自动生产线 v1.0

基于 AI 的英文短篇小说批量生产系统。从题材选品到成稿包装，全自动闭环，附 Web 看板供人为介入。

## 系统架构

```
radar.py            选品：抓 RoyalRoad 榜单 → 空位量化 → 推荐入库
direction_manager   方向：采纳推荐 → 生成方向文件
engine.py           写作：方向 → 大纲 → world bible → 分章(字数硬校验) → 中文摘要
native_check.py     质检：Gemini 母语级审稿(地道+精彩双维度) + 搭配检查
packager.py         包装：发布清单 + EPUB
orchestrator.py     调度：全流程编排 + 日产能/token 限流 + 日结报告
dashboard.py        看板：Web 前端 + 人为介入
progress.py         监测：中间流程进度
blind_review.py     盲评：母语级评委分辨 AI/真人
batch_produce.py    量产：并行生产多部
profiles.py         模型封装：每个模型 + 对应 prompt 包 + 自动路由
state_store.py      SQLite 状态库
```

## 依赖与配置

- Python 3.13（标准库为主，无第三方依赖）
- 模型：火山方舟（写作 deepseek-v4-flash / 审稿 glm-5-2）+ Gemini 3.1-pro（母语级审稿，需代理）
- 配置：`config.json`（模型）、`orchestrator.json`（限流/开关）、`.env`（密钥）、`ai_tells_en.json`（AI味词表）

`.env` 需配置：
```
OPENAI_RELAY_KEY / OPENAI_RELAY_BASE / OPENAI_RELAY_MODEL   # GPT中转(可选)
GEMINI_API_KEY / GEMINI_MODEL / GEMINI_PROXY                # Gemini母语级审稿(需代理)
```

## 快速开始

### 全自动跑一部（一条命令）

```powershell
py -3 orchestrator.py --cycle     # 雷达选品→采纳→写作→质检→包装，全自动
```

### 分步手动跑

```powershell
py -3 radar.py                              # 1. 选品
py -3 direction_manager.py list             # 2. 看推荐
py -3 direction_manager.py adopt            # 3. 采纳 top1
py -3 engine.py --direction-file directions\rec_N.txt --story-id my_story --quality-gate   # 4. 写作(质量门槛)
py -3 native_check.py --story-id my_story --sample 3   # 5. 质检(抽样)
py -3 packager.py --story-id my_story       # 6. 包装
```

### 启动看板（Web 前端）

```powershell
py -3 dashboard.py        # 浏览器打开 http://localhost:8900
```

### 量产

```powershell
py -3 batch_produce.py    # 并行生产 3 部不同题材
```

### 盲评（解黑盒）

```powershell
py -3 blind_review.py     # Gemini 母语级评委分辨 AI/真人
```

## 质量评测体系

| 层 | 模块 | 指标 |
|---|---|---|
| 量化 | quality_probe.py | 字数达标率、AI味密度、Flesch易读性 |
| 审稿 | native_check.py | 地道性(≥3) + 精彩度(≥2) + 搭配检查 |
| 盲评 | blind_review.py | Gemini 分辨 AI/真人（解黑盒） |

评分维度可扩展：在 `native_check.py` 的 `SCORING_SPEC` 追加维度即可。

## 模型 profile 封装（profiles.py）

每个模型绑定一个"包"（backend + prompt 策略 + 输出预算），按模型名自动路由：

| 模型类型 | prompt 包 | 说明 |
|---|---|---|
| thinking 强模型（gemini-3.1-pro） | minimal 精简包 | 约束反而拖累强模型，只给方向+大纲+字数 |
| 轻量/普通模型（flash-lite / 3-flash-preview / deepseek） | full 完整包 | 约束（AI味词表/情感缓冲/去模板化）辅助完成 |
| reasoning 模型（deepseek-v4-pro） | full 完整包 | 接口已留，reasoning 不稳需大 max_tokens |

实测：gemini-3.1-pro 用 full 包 naturalness 3，改用 minimal 包跳到 **5（盲评误判真人）**。

检测端同样封装：`review_with_fallback` 按 reviewers 列表降级（gemini → gpt → glm），GPT 中转恢复后自动接入。

## 关键设计

1. **字数硬校验**：写后数字数，不符带反馈重写（达标率 100%，非 InkOS 软范围）
2. **world bible**：大纲后生成设定圣经（力量体系/世界规则/人物卡），写章逐章对照
3. **母语级质检**：Gemini 3.1-pro 官方 key，能挑物理逻辑/感官合理性错误
4. **盲评回灌闭环**：盲评发现病灶（情感缓冲/模板化/陈词滥调）→ 回灌 prompt → 改进
5. **模型 profile 自动路由**：thinking 模型无约束包，轻量模型完整约束包，按模型名自动切换
6. **质量门槛**：`--quality-gate` 每章写后盲评，naturalness < 4 或字数不足则带反馈重写，确保每章稳定达标（把 3.1-pro 的"偶发 naturalness 5"锁成"稳定 ≥4"）

## 已知限制

1. 反馈闭环（卖得好不好回流）暂缓，依赖营销部门数据对接
2. "读者看不出 AI" 单章验证通过，量产级待确证
3. GPT 中转不稳定（时好时坏），Gemini 需代理访问
4. Windows 计划任务未部署，当前手动触发
