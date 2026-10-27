import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputPath = "E:/novel pro/out/NovelPro_问题总台账_20260916.xlsx";
const font = "Arial";

const marketing = [
  ["M-01", "营销部反馈", "开篇与钩子", "开篇吸引力不足", "男主约在首章860词后才出场；读者能理解女主困境，但未形成足够情绪投入。", "首章至全书转化", "P1", "未解决", "重构开篇信息顺序和冲突触发，不以重复背景代替推进。", "首章全量人工审读；确认角色、风险和主动目标在约定篇幅内出现。", "小说审读反馈_复审版(1).txt"],
  ["M-02", "营销部反馈", "开篇与钩子", "女主损失信息重复", "实验室、资助、样本和权限等信息被反复说明。", "首章节奏", "P1", "未解决", "建立场景信息预算；每条背景只在首次改变决策时说明。", "重复信息扫描与人工节奏审读均通过。", "小说审读反馈_复审版(1).txt"],
  ["M-03", "营销部反馈", "开篇与钩子", "核心冲突展开慢", "危险出现后仍反复讨论合同和离开，行动与代价没有立即升级。", "首章至前三章", "P1", "未解决", "让危险触发不可逆行动或代价，争论必须改变下一步。", "章节事件链显示每场争执产生新约束或新证据。", "小说审读反馈_复审版(1).txt"],
  ["M-04", "营销部反馈", "对白与人声", "对白套路化", "危险、暴力、疼痛后人物仍立即说出完整、锋利的机智句。", "全书人物可信度", "P1", "未解决", "按人物状态约束语言长度、词汇和失误；保留沉默、打断、回避。", "抽样人工审读能区分压力下的语言状态，非仅句式检查。", "小说审读反馈_复审版(1).txt"],
  ["M-05", "营销部反馈", "对白与人声", "人物声音趋同", "女主几乎总占语言优势；多角色都用漂亮话收尾。", "全书角色辨识", "P1", "未解决", "建立角色声音卡和冲突中的失控/让步边界。", "盲读片段可区分主要角色；逐章人声审读无同构结论。", "小说审读反馈_复审版(1).txt"],
  ["M-06", "营销部反馈", "人物反应", "超常事件后的反应失真", "男主展示明显非人力量或暴力后，女主仍主要进行正常谈判或完整专业表达。", "关系可信度", "P0", "未解决", "将认知冲击、恐惧、退避或错误判断写入即时行为，再允许后续恢复理性。", "关键事件逐场人工审读，反应与事件强度匹配。", "小说审读反馈_复审版(1).txt"],
  ["M-07", "营销部反馈", "人物反应", "病情与行动后果不匹配", "男主病情不断加重，但行动能力和剧情后果不成比例。", "人物连续性", "P0", "未解决", "把病情设为可追踪状态，并要求行动、资源和代价随状态变化。", "病情状态表与章节行动逐条对照无矛盾。", "小说审读反馈_复审版(1).txt"],
  ["M-08", "营销部反馈", "感情线", "关系推进依赖命定身体反应", "气味、脉搏、腹部收紧等反应承担主要吸引力，缺少具体欣赏和共同选择。", "感情线", "P1", "未解决", "为关系建立具体观察、合作结果、承诺和后果链。", "每次关系升级能指向前序共同经历与后续行为改变。", "小说审读反馈_复审版(1).txt"],
  ["M-09", "营销部反馈", "叙述", "叙述过度解释", "动作或对白已表达含义，旁白再次解释心理、关系意义和动机。", "全书阅读体验", "P1", "未解决", "审稿定位重复解释，修复时优先删解释并复核因果与信息可读性。", "全书抽检中读者可从行动/对白理解，不依赖解释句。", "小说审读反馈_复审版(1).txt"],
  ["M-10", "营销部反馈", "AI风格", "高频短句和抽象心理总结", "短句、否定转折、停顿和抽象心理总结密集重复。", "全书风格与审核风险", "P0", "未解决", "建立可定位的风格重复检测和人工语境复核，不做全局禁词替换。", "全书扫描给出位置、频率、上下文；复写后复检。", "小说审读反馈_复审版(1).txt"],
  ["M-11", "营销部反馈", "AI风格", "身体反应模板高频", "瞳孔、下颌、嘴角、脉搏、喉咙、抓手腕、黑纹扩散等重复出现。", "全书风格与人物表现", "P0", "未解决", "按角色、情境和相邻章节统计模板簇，并要求替换为行动后果或具体感知。", "模板簇频率下降且修复后人物状态、事实不丢失。", "小说审读反馈_复审版(1).txt"],
  ["M-12", "营销部反馈", "AI风格", "环境拟人模板高频", "房间收紧、森林等待、夜色逼近等环境拟人反复出现。", "全书叙述风格", "P1", "未解决", "统计重复意象和句式，在保留必要氛围时改为具体可感知环境。", "跨章重复意象报告与人工抽检均无密集模板。", "小说审读反馈_复审版(1).txt"],
  ["M-13", "营销部反馈", "情节重复", "同类情节反复执行", "第五章重复变狼、靠近女主、克制、撞守护石、恢复人形。", "中段节奏", "P1", "未解决", "为每章维护不可替代的事件目标、结果和新增约束。", "相邻章节事件矩阵无同一事件结构重复。", "小说审读反馈_复审版(1).txt"],
  ["M-14", "营销部反馈", "情节重复", "既有规则被反复解释", "第六至八章反复解释已经交代的诅咒规则。", "中段节奏", "P1", "未解决", "把规则说明绑定到新证据、新例外或新代价。", "规则提及均能改变判断、行动或风险。", "小说审读反馈_复审版(1).txt"],
  ["M-15", "营销部反馈", "研究线与推进", "研究线没有连续发现链", "反复确认旧发现，缺少问题、证据、排除和新问题的连续推进。", "主线可信度", "P0", "未解决", "维护研究问题状态、证据来源、排除项和未决问题。", "全书研究线可追溯为连续证据链。", "小说审读反馈_复审版(1).txt"],
  ["M-16", "营销部反馈", "关系与后果", "选择没有持续改变后续相处", "前面承诺和选择没有稳定影响后续关系；争执频繁回到女主能否自主行动。", "关系线与章节推进", "P1", "未解决", "将承诺、边界和后果写入跨章状态并在下一场景核验。", "承诺和选择在后续行为、资源或冲突中有可追踪变化。", "小说审读反馈_复审版(1).txt"],
  ["M-17", "营销部反馈", "事实连续性", "明确事实和时间连续性错误", "满月时间、塔维性别、母亲失踪与赠礼、索伦座位、密封箱和走私隧道等前后不一致；历史审读还记录重复脱夹克、重复签合同、未铺垫伤口、倒计时与动作不一致。", "全书交付可信度", "P0", "未解决", "建立统一事实源和章节前后状态对比，阻止未解释矛盾进入交付。", "全书事实审计列出每一处事实的首次建立、后续引用和冲突结果。", "小说审读反馈_复审版(1).txt"],
  ["Z-01", "朱雀检测", "外部审核风险", "AIGC检测风险极高", "朱雀AI检测助手报告：检测文本47,839字符，单片段覆盖100%，AIGC值0.9999，100% AI特征。该指标不是文学质量结论，但构成平台审核和营销交付风险。", "发布、平台审核、营销交付", "P0", "未解决，未复测", "先解决可定位的风格与结构问题；保留同一版本、同一配置下的复测记录，不承诺规避检测。", "修订后同版本复测报告、人工编辑审读和交付决策记录齐全。", "朱雀AI检测助手(1).pdf"]
];

const program = [
  ["P-01", "调度与失败处理", "失败仍可能以退出码0结束", "orchestrator.py 的写作或审稿失败在部分路径未向调用方传递失败。", "orchestrator.py", "P0", "局部修复，未验收", "失败统一写入状态并以非零退出；包装和后继章节必须读取同一终态。", "真实失败注入与CLI退出码测试。"],
  ["P-02", "调度与失败处理", "恢复后续章节只做过单测", "恢复路径的后继解锁逻辑曾出错；修复后没有真实成功链全程验证。", "orchestrator.py / chapter_tasks.py", "P0", "已修代码，未实跑验证", "完成隔离的中断、恢复、全书完成链测试。", "完整run_id下的任务状态、审计和正文产物一致。"],
  ["P-03", "调度与失败处理", "外部调用不稳定", "出现504、429、502、503、426、TLS EOF和长请求超时。", "engine.py / provider配置", "P0", "未解决", "区分协议错误、限流、服务端错误和超时；只重试可重试的受影响工作。", "故障注入记录显示分类、退避、重试和最终终态正确。"],
  ["P-04", "调度与失败处理", "失败成本未完整计入预算", "调用失败后的故事/token预算不能稳定反映真实消耗和剩余额度。", "engine.py / state_store.py", "P1", "未解决", "每次请求以request_id关联用量、重试和预算扣减。", "成功、失败、重试三类运行的预算账与请求日志可对账。"],
  ["P-05", "调度与失败处理", "固定90秒恢复等待放大耗时", "恢复策略采用固定等待，无法根据失败类型缩短或延长。", "orchestrator.py", "P2", "未解决", "按失败类别使用上限、退避和可观测等待，不阻塞无关工作。", "故障演练比较显示等待符合策略且无忙等。"],
  ["P-06", "调度与失败处理", "并行配置与实际执行冲突", "parallel_blocks=2 与为连续性强制串行的实际策略不一致。", "orchestrator.py / config", "P1", "局部修复，未验收", "把配置含义、执行策略和运行报告统一为同一并发模型。", "配置、日志和章节启动顺序一致。"],
  ["P-07", "调度与失败处理", "resume参数曾遗漏", "--resume 漏传曾触发重新生成大纲或世界观。", "orchestrator.py", "P0", "局部修复，未验收", "恢复前校验run_id、故事指纹、状态版本和不可重建资源。", "中断恢复比对显示大纲、世界观和已完成章节哈希未变化。"],
  ["P-08", "调度与失败处理", "孤儿进程污染实验目录", "同一输出目录被遗留进程写入，结果不再可归因。", "orchestrator.py / process管理", "P1", "未解决", "run_id锁、子进程生命周期和输出目录互斥。", "并发/中断演练中无跨run写入。"],
  ["P-09", "状态与审计", "attempt少记内部重写", "chapter_tasks.attempt 未完整记录同一章节内部重写次数。", "chapter_tasks.py / state_store.py", "P1", "未解决", "把生成、修复、复审分为可审计尝试事件。", "一次多轮修复可从日志重建完整时间线。"],
  ["P-10", "状态与审计", "旧schema迁移未完整验证", "旧状态文件到新schema的迁移缺少真实样本覆盖。", "state_store.py", "P1", "未解决", "为历史schema保留迁移夹具与不可逆字段检查。", "所有存档schema样本迁移后可恢复并通过一致性检查。"],
  ["P-11", "状态与审计", "非法状态转移未严格阻断", "任务状态机未全面拒绝非法跳转。", "chapter_tasks.py", "P0", "未解决", "定义状态转换表并在单点写入处强校验。", "转移矩阵测试覆盖允许和禁止边。"],
  ["P-12", "状态与审计", "writing状态可能保留旧错误字段", "重新写作时残留failure_type或warning，状态含义不可靠。", "writing_status.py / state_store.py", "P1", "未解决", "写入新状态时显式清除不适用字段并记录事件历史。", "失败后重试成功的状态快照无陈旧字段。"],
  ["P-13", "状态与审计", "进度与数据库残留过期运行状态", "曾出现running、6/12等过期进度，不能代表当前完整运行。", "writing_status.py / DB", "P0", "未解决", "以run_id和心跳/终态管理进度，过期运行可检测并隔离。", "中断后状态恢复演练不出现伪完成或伪运行。"],
  ["P-14", "状态与审计", "新旧书状态文件不统一", "旧书和新书使用的状态、审计和产物口径不同。", "状态文件与历史产物", "P1", "历史遗留待复检", "定义版本化交付清单和统一状态适配层。", "选取旧书和新书均可生成同结构交付审计。"],
  ["P-15", "状态与审计", "单章1/1可被误当整书完成", "单章运行显示1/1完成，缺少与目标全书章节数的明确区分。", "orchestrator.py / reporting", "P0", "未解决", "报告同时显示目标范围、完成范围和发布资格。", "单章、部分书、全书三种运行的报告不可混淆。"],
  ["P-16", "事实与跨章连续性", "没有统一事实源", "新故事未自动建立人物、地点、物品、时间、伤势、承诺、研究结论的结构化真源。", "story facts / generation pipeline", "P0", "未解决", "在生成前建事实模型，章节写作和修复只读取/更新可审计的事实状态。", "全书事实表能链接到首次建立与后续使用的位置。"],
  ["P-17", "事实与跨章连续性", "全书审稿实际逐章独立", "审稿不能稳定捕捉跨多章的因果、承诺和事实冲突。", "native_check.py / review flow", "P0", "未解决", "增加全书级连续性审读，并以章节状态连接相邻与远距引用。", "全书审读覆盖全部章节并输出按事实ID聚合的问题。"],
  ["P-18", "事实与跨章连续性", "章节边界状态验证不可靠", "上一章结束状态到下一章开头没有稳定的自动比对。", "chapter_state.py / prompts", "P0", "未解决", "定义章节状态合同，写入、读取和复核均使用同一结构。", "边界测试能抓住时间、位置、伤势、物品和承诺冲突。"],
  ["P-19", "事实与跨章连续性", "state contract未证明有效", "Terra对照中合同版65个问题、基线56个问题；Route B未完成目标章节。", "实验结果", "P0", "未解决", "先固定输入、模型、审稿器与完成条件，再做可比完整实验。", "预注册指标下的完整对照运行和审计文件。"],
  ["P-20", "审稿与质量门禁", "盲评覆盖不足", "盲评只抽头900和尾600字符，中段不覆盖。", "native_check.py", "P0", "未解决", "按章节全量或可复现分层抽样审读，覆盖中段与转折点。", "审读报告列出每章覆盖范围和发现位置。"],
  ["P-21", "审稿与质量门禁", "连续性blocker依赖固定英文标签", "换词或不符合标签格式的矛盾可能漏报。", "native_check.py", "P0", "局部修复，未验收", "以事实状态比对和证据定位为主，标签仅作呈现层。", "同义改写、中文表达和跨章矛盾样本均被捕捉。"],
  ["P-22", "审稿与质量门禁", "评分门槛被误当无问题", "分数达标不表示没有事实、叙事或风格问题；模型和调用失败也会改变报告口径。", "native_check.py / reporting", "P0", "未解决", "把分数、阻断项、覆盖范围、失败率和人工结论分开呈现。", "交付报告不以单一分数宣称无问题。"],
  ["P-23", "审稿与质量门禁", "营销反馈没有程序级验收", "开篇、对白、人声、推进和AI风格没有对应的全书验收门禁。", "review flow / prompts", "P0", "未解决", "将已归类反馈转为可定位检查、人工复核与发布条件。", "每个营销问题有检测记录、修复记录和复检结论。"],
  ["P-24", "审稿与质量门禁", "主观模板禁令可能压制正常文风", "纯词表或频次禁令可能删掉合理表达，且无法保证整体质量。", "style rules / repair prompts", "P1", "未解决", "用重复簇、上下文与角色功能判断，禁止无锚点全局替换。", "人工复核显示修复未造成语义、人物声音或文风损失。"],
  ["P-25", "审稿与质量门禁", "局部修复只验证规则消失", "未验证修复是否保留事实、因果和段落功能。", "repair_pipeline.py", "P0", "局部修复，未验收", "修复后同时做事实diff、相邻上下文审读和原规则复检。", "每个修复有前后文本、事实状态和复检结果。"],
  ["P-26", "修复闭环", "审稿失败未进入完整闭环", "当前可生成repair cards，但未形成发现、定位、最小修复、事实对比、复检、通过/回滚的统一流程。", "repair_queue.py / repair_pipeline.py", "P0", "未解决", "建立问题生命周期和不可跳过的复检门。", "一个失败样本从发现到闭环的记录完整且可回放。"],
  ["P-27", "修复闭环", "修复证据未统一关联", "正文哈希、事实状态、审稿报告和修复前后版本没有统一关联键。", "repair artifacts / state_store.py", "P0", "未解决", "以run_id、chapter_id、revision_id建立版本与证据关系。", "任一交付章节可追溯全部生成、修复和复检证据。"],
  ["P-28", "修复闭环", "历史局部修复未证明收敛", "至少8次repair apply后，后续审稿仍发现大量问题。", "历史repair产物", "P1", "历史遗留待复检", "按问题类型测量净新增、复发和关闭率，而不是只计应用次数。", "连续批次显示问题收敛或明确停止条件。"],
  ["P-29", "修复闭环", "问题层级未严格区分", "事实、确定性局部问题、语义、结构和风格问题使用混杂修复策略。", "repair_queue.py / prompts", "P1", "未解决", "按风险分层路由：事实优先，局部确定性修复受锚点约束，结构问题人工/全章处理。", "各类问题使用对应修复与验收路径。"],
  ["P-30", "摘要、包装与交付", "摘要输入曾只取前5000字符", "摘要偏向首章，无法代表全书。", "packager.py / summary pipeline", "P1", "未解决", "按章节覆盖、结构节点和结局状态生成摘要。", "摘要证据显示覆盖所有章节与关键转折。"],
  ["P-31", "摘要、包装与交付", "包装产物不等于编辑可交付", "EPUB、manifest和full.md存在不代表正文通过完整审读。", "packager.py / publish manifest", "P0", "未解决", "发布资格必须关联全书审读、问题闭环、版本和外部风险记录。", "manifest仅在全部发布门禁通过后生成release状态。"],
  ["P-32", "摘要、包装与交付", "交付物缺失或最终版本不清", "standalone_romance_01_full_20260902曾缺summary_cn.md；旧书修复产物多，最终可信正文版本不清。", "历史交付目录", "P1", "历史遗留待复检", "交付清单、正文主版本和派生产物使用同一版本指纹。", "交付目录可自动校验完整性和版本一致性。"],
  ["P-33", "摘要、包装与交付", "发布状态未纳入营销和AI检测", "营销反馈、朱雀检测与程序质量门禁没有统一进入发布判断。", "publish manifest / release flow", "P0", "未解决", "建立发布门禁，列出编辑审读、连续性、风格风险和外部检测的状态。", "发布记录能显示每项门禁的证据与例外批准。"],
  ["P-34", "实验与结论管理", "不同模型与审稿器混比较", "模型、参数、审稿器改变后仍被横向比较，归因不成立。", "实验记录", "P1", "未解决", "记录不可变实验清单，未控制变量的结果只作为探索性观察。", "比较报告列出固定与变化变量。"],
  ["P-35", "实验与结论管理", "默认审稿输出曾被覆盖", "结果文件覆盖导致无法复核原始审稿证据。", "实验输出目录", "P1", "未解决", "每次运行使用唯一目录与不可覆盖原始输出。", "重跑不会覆盖既有报告，索引可定位原始文件。"],
  ["P-36", "实验与结论管理", "未完成实验被当成有效结论", "Route B未完成要求章节，不能用于证明策略有效。", "实验结论", "P0", "未解决", "把完成条件、缺失产物和结论资格写入实验状态。", "未满足条件的实验报告自动标为不作比较结论。"],
  ["P-37", "实验与结论管理", "吞吐比较混入字数和失败次数", "不同字数、失败与重试被混入速度比较。", "实验指标", "P2", "未解决", "分别报告成功章节、字符数、请求数、失败和墙钟时间。", "吞吐报告可按统一分母复算。"],
  ["P-38", "实验与结论管理", "代理指标被外推为留存因果", "数据可提供题材或开篇钩子参考，不能证明留存因果。", "实验结论", "P1", "未解决", "在结论中区分描述性相关、质量代理和真实用户结果。", "报告中每个结论匹配其证据等级与限制。"]
];

const marketingSources = "人工复审：C:\\Users\\唐杰\\Documents\\WXWork\\1688856408827045\\Cache\\File\\2026-09\\小说审读反馈_复审版(1).txt\n朱雀报告：C:\\Users\\唐杰\\Documents\\WXWork\\1688856408827045\\Cache\\File\\2026-09\\朱雀AI检测助手(1).pdf";

function setupSheet(sheet, title, subtitle, headers, rows, widths, tableName, sourceText = null) {
  sheet.getRange("A1:K1").merge();
  sheet.getRange("A1").values = [[title]];
  sheet.getRange("A2:K2").merge();
  sheet.getRange("A2").values = [[subtitle]];
  sheet.getRange(`A4:${String.fromCharCode(64 + headers.length)}4`).values = [headers];
  sheet.getRange(`A5:${String.fromCharCode(64 + headers.length)}${rows.length + 4}`).values = rows;
  const endCol = String.fromCharCode(64 + headers.length);
  sheet.tables.add(`A4:${endCol}${rows.length + 4}`, true, tableName).style = "TableStyleMedium2";
  sheet.getRange(`A1:${endCol}1`).format = { fill: "#17365D", font: { name: font, size: 15, bold: true, color: "#FFFFFF" }, horizontalAlignment: "left", verticalAlignment: "center" };
  sheet.getRange(`A2:${endCol}2`).format = { font: { name: font, size: 10, italic: true, color: "#5B6573" }, horizontalAlignment: "left", verticalAlignment: "center" };
  sheet.getRange(`A4:${endCol}4`).format = { fill: "#1F4E78", font: { name: font, size: 10, bold: true, color: "#FFFFFF" }, horizontalAlignment: "center", verticalAlignment: "center", wrapText: true };
  sheet.getRange(`A5:${endCol}${rows.length + 4}`).format = { font: { name: font, size: 10, color: "#1F2937" }, verticalAlignment: "top", wrapText: true };
  sheet.getRange("A1").format.rowHeight = 26;
  sheet.getRange("A2").format.rowHeight = 31;
  sheet.getRange("A4").format.rowHeight = 34;
  for (let i = 0; i < widths.length; i++) sheet.getRange(`${String.fromCharCode(65 + i)}:${String.fromCharCode(65 + i)}`).format.columnWidth = widths[i];
  for (let r = 5; r <= rows.length + 4; r++) sheet.getRange(`A${r}:${endCol}${r}`).format.rowHeight = 62;
  sheet.getRange(`A5:A${rows.length + 4}`).format = { font: { name: font, size: 10, bold: true, color: "#17365D" }, verticalAlignment: "top", wrapText: true };
  sheet.getRange(`G5:G${rows.length + 4}`).format = { font: { name: font, size: 10, bold: true }, horizontalAlignment: "center", verticalAlignment: "top", wrapText: true };
  sheet.getRange(`H5:H${rows.length + 4}`).format = { font: { name: font, size: 10, bold: true }, horizontalAlignment: "center", verticalAlignment: "top", wrapText: true };
  sheet.getRange(`G5:G${rows.length + 4}`).conditionalFormats.addCustom('=$G5="P0"', { fill: "#FCE4D6", font: { color: "#9C0006", bold: true } });
  sheet.getRange(`G5:G${rows.length + 4}`).conditionalFormats.addCustom('=$G5="P1"', { fill: "#FFF2CC", font: { color: "#7F6000", bold: true } });
  sheet.getRange(`G5:G${rows.length + 4}`).conditionalFormats.addCustom('=$G5="P2"', { fill: "#E2F0D9", font: { color: "#375623", bold: true } });
  sheet.getRange(`H5:H${rows.length + 4}`).conditionalFormats.addCustom('=ISNUMBER(SEARCH("未解决",$H5))', { fill: "#FCE4D6", font: { color: "#9C0006", bold: true } });
  sheet.getRange(`H5:H${rows.length + 4}`).conditionalFormats.addCustom('=ISNUMBER(SEARCH("未验收",$H5))', { fill: "#FFF2CC", font: { color: "#7F6000", bold: true } });
  sheet.freezePanes.freezeRows(4);
  sheet.freezePanes.freezeColumns(2);
  sheet.showGridlines = false;
  if (sourceText) {
    const sourceRow = rows.length + 7;
    sheet.getRange(`A${sourceRow}`).values = [["来源文件"]];
    sheet.getRange(`A${sourceRow}`).format = { font: { name: font, size: 10, bold: true, color: "#17365D" } };
    sheet.getRange(`B${sourceRow}:${endCol}${sourceRow + 1}`).merge();
    sheet.getRange(`B${sourceRow}`).values = [[sourceText]];
    sheet.getRange(`B${sourceRow}`).format = { font: { name: font, size: 9, color: "#4B5563" }, wrapText: true, verticalAlignment: "top" };
    sheet.getRange(`B${sourceRow}`).format.rowHeight = 42;
  }
}

const workbook = Workbook.create();
const ledger = workbook.worksheets.add("总问题台账");
const marketingSheet = workbook.worksheets.add("营销部反馈");
const programSheet = workbook.worksheets.add("程序问题");

const masterHeaders = ["ID", "来源", "问题分类", "问题", "具体表现或证据", "影响范围", "优先级", "当前状态", "需要的解决方向", "验收证据", "关联模块或来源文件"];
const masterRows = [
  ...marketing.map(r => [r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10]]),
  ...program.map(r => [r[0], "程序审计/历史实跑", r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9]])
];
setupSheet(ledger, "Novel Pro 问题总台账", "汇总营销部两份反馈与程序审计/历史实跑问题。状态反映当前证据，不等同于整书或系统验收。", masterHeaders, masterRows, [10, 18, 18, 28, 52, 20, 10, 18, 38, 38, 30], "ProblemLedgerTable");

const marketingHeaders = ["ID", "来源", "问题分类", "问题", "具体表现或证据", "影响范围", "优先级", "当前状态", "需要的解决方向", "验收证据", "来源文件"];
setupSheet(marketingSheet, "营销部反馈与朱雀检测", "人工复审反馈和朱雀AI检测报告的逐项问题。朱雀结果属于对外审核风险，不作为文学质量的唯一结论。", marketingHeaders, marketing, [10, 16, 18, 28, 52, 20, 10, 18, 38, 38, 30], "MarketingIssuesTable", marketingSources);

const programHeaders = ["ID", "问题分类", "问题", "具体表现或证据", "关联模块", "优先级", "当前状态", "需要的解决方向", "验收证据"];
programSheet.getRange("A1:I1").merge();
programSheet.getRange("A1").values = [["Novel Pro 程序问题"]];
programSheet.getRange("A2:I2").merge();
programSheet.getRange("A2").values = [["来自当前代码、历史实跑和实验结论的程序级问题。已修代码不等于已通过真实全链路验收。"]];
programSheet.getRange("A4:I4").values = [programHeaders];
programSheet.getRange(`A5:I${program.length + 4}`).values = program;
programSheet.tables.add(`A4:I${program.length + 4}`, true, "ProgramIssuesTable").style = "TableStyleMedium2";
programSheet.getRange("A1:I1").format = { fill: "#17365D", font: { name: font, size: 15, bold: true, color: "#FFFFFF" }, horizontalAlignment: "left", verticalAlignment: "center" };
programSheet.getRange("A2:I2").format = { font: { name: font, size: 10, italic: true, color: "#5B6573" }, horizontalAlignment: "left", verticalAlignment: "center" };
programSheet.getRange("A4:I4").format = { fill: "#1F4E78", font: { name: font, size: 10, bold: true, color: "#FFFFFF" }, horizontalAlignment: "center", verticalAlignment: "center", wrapText: true };
programSheet.getRange(`A5:I${program.length + 4}`).format = { font: { name: font, size: 10, color: "#1F2937" }, verticalAlignment: "top", wrapText: true };
programSheet.getRange("A1").format.rowHeight = 26;
programSheet.getRange("A2").format.rowHeight = 31;
programSheet.getRange("A4").format.rowHeight = 34;
const pWidths = [10, 18, 28, 52, 28, 10, 20, 38, 38];
for (let i = 0; i < pWidths.length; i++) programSheet.getRange(`${String.fromCharCode(65 + i)}:${String.fromCharCode(65 + i)}`).format.columnWidth = pWidths[i];
for (let r = 5; r <= program.length + 4; r++) programSheet.getRange(`A${r}:I${r}`).format.rowHeight = 62;
programSheet.getRange(`A5:A${program.length + 4}`).format = { font: { name: font, size: 10, bold: true, color: "#17365D" }, verticalAlignment: "top", wrapText: true };
programSheet.getRange(`F5:F${program.length + 4}`).format = { font: { name: font, size: 10, bold: true }, horizontalAlignment: "center", verticalAlignment: "top", wrapText: true };
programSheet.getRange(`G5:G${program.length + 4}`).format = { font: { name: font, size: 10, bold: true }, horizontalAlignment: "center", verticalAlignment: "top", wrapText: true };
programSheet.getRange(`F5:F${program.length + 4}`).conditionalFormats.addCustom('=$F5="P0"', { fill: "#FCE4D6", font: { color: "#9C0006", bold: true } });
programSheet.getRange(`F5:F${program.length + 4}`).conditionalFormats.addCustom('=$F5="P1"', { fill: "#FFF2CC", font: { color: "#7F6000", bold: true } });
programSheet.getRange(`F5:F${program.length + 4}`).conditionalFormats.addCustom('=$F5="P2"', { fill: "#E2F0D9", font: { color: "#375623", bold: true } });
programSheet.getRange(`G5:G${program.length + 4}`).conditionalFormats.addCustom('=ISNUMBER(SEARCH("未解决",$G5))', { fill: "#FCE4D6", font: { color: "#9C0006", bold: true } });
programSheet.getRange(`G5:G${program.length + 4}`).conditionalFormats.addCustom('=ISNUMBER(SEARCH("未验收",$G5))', { fill: "#FFF2CC", font: { color: "#7F6000", bold: true } });
programSheet.freezePanes.freezeRows(4);
programSheet.freezePanes.freezeColumns(2);
programSheet.showGridlines = false;

workbook.recalculate();
const summaryCheck = await workbook.inspect({ kind: "table", range: "总问题台账!A1:K12", include: "values,formulas", tableMaxRows: 12, tableMaxCols: 11 });
const marketingCheck = await workbook.inspect({ kind: "table", range: "营销部反馈!A1:K12", include: "values,formulas", tableMaxRows: 12, tableMaxCols: 11 });
const programCheck = await workbook.inspect({ kind: "table", range: "程序问题!A1:I12", include: "values,formulas", tableMaxRows: 12, tableMaxCols: 9 });
const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!", options: { useRegex: true, maxResults: 300 }, summary: "final formula error scan" });
console.log("SUMMARY_CHECK\n" + summaryCheck.ndjson);
console.log("MARKETING_CHECK\n" + marketingCheck.ndjson);
console.log("PROGRAM_CHECK\n" + programCheck.ndjson);
console.log("ERROR_SCAN\n" + errors.ndjson);

await fs.mkdir("E:/novel pro/out", { recursive: true });
for (const [sheetName, range, filename] of [
  ["总问题台账", "A1:K20", "E:/novel pro/out/NovelPro_问题总台账_preview.png"],
  ["营销部反馈", "A1:K20", "E:/novel pro/out/NovelPro_营销部反馈_preview.png"],
  ["程序问题", "A1:I20", "E:/novel pro/out/NovelPro_程序问题_preview.png"]
]) {
  const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
  await fs.writeFile(filename, new Uint8Array(await preview.arrayBuffer()));
}
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);
console.log(`OUTPUT=${outputPath}`);
