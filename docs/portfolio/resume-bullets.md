# 简历表述

## 中文项目经历（可直接使用）

**Storymill｜AI 英文短篇小说生产与质量控制引擎**

- 独立设计并实现 Python 生产管线，将选题雷达、方向采纳、大纲、world bible、分章写作、章节验收、质量检查、问题修复和 EPUB 包装串联为可恢复流程。
- 针对长请求被上游网关约 60 秒截断的问题，引入流式接收、空闲超时、失败分类和调用审计；历史长请求由约 60 秒失败改为约 125 秒完成。
- 设计章节合同、场景账本、事实槽位、人物声音卡和状态交接，把“生成正文”和“章节满足要求”拆成独立阶段；失败章节保留候选稿和原因，不直接落盘为完成。
- 实现确定性质量检查、质量门禁、锚定修复队列和问题账本，支持按正文哈希、原文锚点、修改范围和复查状态追踪问题闭环。
- 完成一份 12 章英文样稿（约 30,474 词）和 EPUB/发布清单包装；当前回归测试 375 项通过，并保留已知质量限制和未完成的真实端到端验证。

## English resume version

**Storymill | AI English Short-Fiction Production and Quality-Control Engine**

- Designed and implemented a Python production pipeline covering trend/radar ingestion, direction selection, outlining, world-bible generation, chapter writing, chapter acceptance, quality checks, repair tracking, and EPUB packaging.
- Reworked long-running model calls around streaming responses, idle timeouts, failure classification, retry boundaries, and call-level audit records; a historical request that failed near the upstream 60-second gateway limit completed in roughly 125 seconds after the change.
- Built chapter contracts, scene ledgers, fact slots, character voice cards, and state handoffs so that generated prose and chapter acceptance are separate, inspectable stages.
- Implemented deterministic text checks, an evidence-driven quality gate, anchored repair cards, source-hash verification, and an issue ledger that preserves resolved history and reopens recurring findings.
- Produced and packaged a 12-chapter English sample of approximately 30,474 words; the current working tree passes 375 regression tests, with remaining end-to-end and editorial-readiness limits documented.

## 面试时的 30 秒版本

这是一个我独立做的 AI 写作工程系统，重点不是调用模型生成文字，而是把模型调用变成一条有状态、可恢复、可验收的生产流程。我处理过长请求网关超时、断点恢复、章节合同、事实一致性、确定性文本检查和修复账本，并用真实 12 章样稿和 375 项回归测试留下证据。同时我把“代码跑通”“产物生成”“编辑质量合格”分开报告，避免把自动化结果夸大成出版质量。

## 不建议写在简历上的表述

- “稳定批量生产高质量小说”
- “生成内容可以直接发布”
- “所有质量问题已自动解决”
- “模型盲评通过等于编辑验收通过”

这些说法都超出当前仓库保存的证据范围。
