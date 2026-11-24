# 应急演练记录合规审查（对话工具 · 每周任务 · 页面）

链路：定位演练周 → 下载该周 4 个班组（A/B/C/D）演练记录 PDF（**扫描件**）→ MinerU OCR（本地 Gradio 优先、云端兜底）→
检索知识库对应 EOP（应急操作流程）→ **每个班组各自独立一次 LLM 比对** →
周级统计与各组一句话确定性拼接 → 结构化结论（`groups[]` + `issues` + `summary` + `note`）
+ 对话内可交互附件。
**不做「合规 / 不合规」档位判定**：只给每组综合评价、问题清单与按严重程度的统计，
个别小问题不等于整周演练不合格。
**按维度判、重点查漏（不是抠文字字眼）**：以 EOP 为唯一标尺，把 §9.3 应急实施步骤拆成
可勾对的清单，逐条在演练时间轴里语义匹配——EOP 有、演练实际没做的步骤即「漏做」，进
`missing_steps` 并镜像进问题清单；同时对岗位分配 / 步骤完整性 / 时限执行 / 操作合规 /
关键项五个维度各给 `达标 / 偏差 / 缺失 / 无法判定`，时间轴每步标注累计/本步耗时与
超时/违规标记。见「EOP 检索与判定」。
原始 OCR 文本**不进模型上下文**，只回评价与证据摘录；PDF 与 OCR 文本落**本地内容库**
（内容哈希寻址、按容量淘汰，见「内容存储与缓存」），Mongo 只存索引与周记录。

目录 `service/drill_audit/`，工具 `tools/drill_audit_tools.py::drill_compliance_audit`，
技能 `skills/drill_audit/SKILL.md`，开关 `DRILL_AUDIT_ENABLED`（默认 false）。
除对话入口外，还接了**每周定时任务**（`drill_audit__weekly`，默认周一 07:10）与
**合规检查页第五张卡 + 整屏看板**，见「每周任务与页面」。

## 接口

| 用途 | 接口 |
|------|------|
| 计划列表（含 `weeks[]`） | `GET /drill/plan/list/v1?deptId&year`（纯数组） |
| 演练周详情（班组文件） | `GET /drill/plan/week/v1/{week_id}` |
| 下载班组记录 PDF | `GET /drill/plan/download/v1/{file_id}`（原始字节，走 `mgmt.http.download_bytes`） |

上表前两个接口同时声明为 mgmt 平台能力 `mgmt.query_drill_plans` / `mgmt.query_drill_week`
（`service/query/mgmt/capabilities.py` + `endpoints.py`）：`MGMT_ENABLED=true` 时对话侧可直接
「查演练计划 / 查某周安排」，走 `tool_search` 与智能体工具目录，不受 `DRILL_AUDIT_ENABLED` 影响；
本审查流水线不变，仍只走 `service/drill_audit/fetch.py`。对话侧 `mgmt.query_drill_plans` 是
组合能力（`client._query_drill_plans`）。

**部门定位：先定公司，再取部门。**「A18」「技术保障部」是名称不是 id，且 A1/A18 这类名称
**在多家公司同名**（A1分部在润泽/海外/平湖/佛山/重庆 5 家公司都有）。`query_dept_by_name`
（`GET /dept/v1/dept/{name}`）只返回同名部门中的一条 —— 实测 `A1` 返回的是**海外园区**的
A1分部 `2EW1DTq1awF`（该部门与其父部门都没有任何演练计划），而廊坊真正的 A1 是润泽科技下的
`23BaepxLzUZ`（53 条计划），**因此不能拿它做跨公司 dept_id 解析**。正确做法：

- 直接给 `query_drill_plans` 传 `dept_name='A1'` + `park='廊坊'`，由服务端
  `client._resolve_dept_id` 确定性解析（`query_company_list` 按 `gardenName` 收窄公司 →
  逐公司 `query_dept_list(company_id)` 精确匹配简称/全名，均走 24h 静态缓存）；
  0 个命中或多公司同名都返回 `{"plans": [], "note": …}` 让模型追问，**不猜**。
- 或由模型自己先 `query_company_list` 定位公司、再 `query_dept_list(company_id)` 选 dept_id。
- 省略 `dept_name`/`dept_id` 时用本人部门（服务账号回退技术保障部）。

其余参数：问「本月/某月」传 `month`、「第 N 周」传 `week`、要已安排/未完成等传
`status`/`finish_status`、「11 月下旬」传 `start_date`+`end_date`；按名称定位具体计划传
`plan_name`（去空白子串匹配，中文名常带空格）；**`status` 未传时默认只返回已安排周**
（问「哪些周没安排」显式传 `status=1`）。服务端按 `weeks[]` 收窄（日期区间按 stime/etime
重叠判断），避免全年约 50 周撑过工具输出上限、末尾计划被整条截断，也让模型不必在长 JSON 里
靠眼睛找周次。

要点：`POST /file/v1/download/{fid}` 返回 `data:null`；预签名 URL 指向的 bucket 对象不存在
（404 NoSuchKey）——只有 `download/v1` 能取到真实 PDF。周详情里班组字段前缀是小写字母
（`agroupFileId`/`bgroupFileId`…）；`status=0` 才是「已安排」。

## 解析

演练记录是扫描图片 PDF（`extract_text()` 为 0 字符），必须 OCR。`parse.py` 的选路：
`DRILL_AUDIT_LOCAL_OCR`（默认 true）且配了 `MINERU_GRADIO_URL` 时优先调
`repository.ingestion.loader._load_pdf_mineru_gradio(path, is_ocr=True)`
（本地 Gradio，backend 默认 vlm-engine，实测 8~10s、扫描件覆盖率最好），异常记 warning 后
回退云端 `_load_pdf_mineru_api(path, is_ocr=True, language='ch')`
（云端 MinerU v4，`is_ocr` 对 vlm/pipeline 模型生效，实测 2 页约 30s）；
**不能**调 `_load_pdf_uncached` —— 它的本地 CLI 兜底会挂 600s，超过 `AGENT_EVENT_TIMEOUT=360`。
单份超时 `DRILL_AUDIT_OCR_TIMEOUT`（180s），失败只把该班组标进 `note`，其余照常判定。

## 内容存储与缓存

同一演练周的班组记录在提交后基本不变，而下载 + OCR 一份约 30s。存两份东西：**内容**（PDF 字节 +
OCR 文本，按内容寻址）与**周记录**（结论与元数据，按 `week_id`）。命中内容库则**零下载零 OCR**。

### 结构

```
data/drill_audit/sha256/<sha 前 2 位>/<sha256>.pdf
data/drill_audit/sha256/<sha 前 2 位>/<sha256>.txt
```

`data/` 已在 `.gitignore`。内容寻址：同一份 PDF 换 `file_id` 重传也只存一份；写入走
`mkstemp + os.replace` 原子替换。文件 IO 走 `asyncio.to_thread`，不阻塞事件循环。

Mongo（`config.mongodb_conn.mongodb_manager`）：

- `drill_audit_files` —— 一条 = 一份文件（`_id = file_id`）：`sha256`、`size_pdf`/`size_text`、
  `chars`、`pdf_path`/`text_path`、`pdf_present`/`text_present`、`status`（`ok`/`ocr_failed`/`evicted`）、
  `dept_id`/`plan_id`/`week_id`/`letter`、`first_seen_at`/`last_used_at`。
- `drill_audit_weeks` —— 一条 = 一个演练周（`_id = week_id`）：计划/专业/部门/周次/起止/完成状态、
  `groups[]`（每班组 file_id/文件名/状态/字数/问题数/严重程度统计/综合评价 +
  `dimensions[]`/`missing_steps[]`/`step_findings[]`/`timeline_stats{}`，见判定节）、
  `issues`/`summary`/`note`、`eop_refs`、
  `eop_fingerprint`（命中文件名排序拼接，仅用于展示结论基于哪批 EOP，**不作跳过重算的条件**）、
  `audit_source`（`dialog` / `weekly`）。
- `drill_audit_plans` —— 一条 = 一个「部门 × 年 × 计划」（`_id = {dept_id}|{year}|{plan_id}`）：
  计划名/专业/部门/公司 + `weeks[]`（只留 `status=0` 已排期周的 `week_id`/`week_no`/`key`/`stime`/`etime`）。
  **扫描时按 (dept_id, year) 整批替换**（逐条 upsert 后删掉本次接口没返回的计划），
  供看板列出全部演练任务；读写失败只记日志（fail-open）。

### 命中口径

- `store.load_text(file_id)` 需要 `status == 'ok'` 且 `text_present` 且 txt 文件**实际存在**；
  命中时更新 `last_used_at`。
- 上游换文件必然换 `file_id` → 旧缓存条目自然不会被误命中。
- OCR 失败写 `status='ocr_failed'`（**不缓存失败**，下次重试），不写空文本。
- 所有 Mongo/磁盘异常只 `logger.warning`，当未命中/未写入——**缓存问题绝不让审查失败**。

### 容量淘汰（不设期限，只按容量）

- 入口：每轮审查结束调一次 `store.evict_if_needed()`；容量取 `$sum(size_pdf + size_text)`，
  上限 `DRILL_AUDIT_STORE_MAX_MB`。
- 顺序：**先删 PDF**（数 MB），仍超再删 TXT（几十 KB）——保住最贵的 OCR 成果。
- 粒度：按 `last_used_at` 升序（LRU），删到上限的 90% 为止，避免压线反复触发。
- 淘汰只删**内容文件**、保留元数据（标 `pdf_present`/`text_present=false`），下次自动重采。

### 刷新与采集

- `refresh=True` 绕过内容库读取、重下重解析（怀疑上游记录已被替换/更新时用）；工具侧同名参数。
- `audit=False` 只采集文本、不检索 EOP 也不判定——未来「每周演练合规检查」批量采集时用
  （`service/drill_audit/collect.py::collect_week` 是对话与周任务共用的入口）。

### 已知盲区

- 上游若复用同一 `file_id` 替换内容，检测不到（只认 `file_id`；靠 `refresh=True` 兜）。
- 淘汰后首次查询会重新下载 + OCR（设计内代价，不是故障）。

## EOP 检索与判定

- `retrieve.find_eop(query)`：`rag_service.hybrid_retrieve(..., intent_filter={'doc_type': 'EOP'})`，
  再按 `metadata['doc_type'] == 'EOP'` 过滤（fail-soft 退化到全库时丢掉非 EOP 文档），取前
  `DRILL_AUDIT_EOP_TOP_K` 条。检索不到 → 各组标「无法评价」+ `note`，**不调模型**、不用常识替代。
- **检索链路本身不可用**（Milvus 连不上等）抛 `retrieve.EopRetrievalError`，**不能伪装成「无命中」**：
  `pipeline` 捕获后 `note` 写「知识库检索不可用（Milvus 连接失败），本次未能评价」，同样不调模型。
  两者必须区分——「知识库无相关 EOP 文档」是数据问题，「检索不可用」是环境问题。
- **EOP 标尺提取**（`eop_spec.py`，纯函数）：`highlight_lines(eop_docs)` 容错抽 §9.1/§9.2/
  §9.3.x/§10.x 这类带执行人/时间限定的行（排除 `9.10/9.11` 被误当章号、排除目录点线行），
  全角归一、去重，把「岗位/时限/步骤」标尺高亮置顶喂模型，避免在 EOP 预算里被无关正文挤掉；
  `extract_steps(eop_docs)` 从 §9.3 拆出**一条条必需步骤** `{no, action, executor, location,
  time_limit}`（同一行/分行两种形态都认，解析不到的字段留空**不编造**），作为步骤完整性
  的勾对清单。无 §9.3 可结构化时返回 `[]`，judge 回退原始 EOP 判、并把该维度标注为「仅粗判」。
- **每班组独立判定**：`judge.audit_group(plan, letter, text, eop_docs)` 只喂本班组记录
  （`【X班组演练记录】`，**不截断**）+ 压缩后的时间轴（`seq / 累计秒 / 本步秒 / 车道 / 摘要`），
  一次 `model_gateway.chat(json_mode=True, temperature=0, max_tokens=3000,
  caller='drill_audit')`。**不输出合规档位**，按 5 个有序维度（`role_assignment` 岗位分配 /
  `step_completeness` 步骤完整性 / `time_limit` 时限执行 / `compliance` 操作合规 /
  `critical_items` 关键项）各给 `达标 / 偏差 / 缺失 / 无法判定`，并输出 `missing_steps`
  （EOP 有、演练缺的步骤）、`step_findings`（演练里存在的、做错的步：超时/违规）、
  `issues`、`summary`（2~4 句综合评价，个别细节偏差不得否定整组）。每条结论必须引 EOP 依据。
- **归一化（服务端确定性，不信模型算术/抄写）**：`dimensions` 始终补齐 5 个 key（缺→`无法判定`），
  `偏差/缺失` 但 `basis` 落不到 EOP 文本里 → 降 `无法判定`。`missing_steps` 只保留能对上
  `extract_steps()` 清单或 EOP 原文的条目，`executor/time_limit` 从清单回填，每条**镜像**进
  `issues`（`dimension=step_completeness`）保证问题清单不漏。`step_findings` 的 `seq` 必须落在
  有效范围、`actual` 由服务端按 `metric`（累计/本步）从时间轴**覆盖**、`limit` 须在 EOP 印证、
  `status` 只接受 `超时/违规`，上限 12 条。每条问题必须带 `eop_basis` 与 `drill_evidence`，
  缺依据的条目在解析时丢弃，并统一注入 `group`（班组字母）。**输出解析不出有效结论时
  （JSON 被 `max_tokens` 截断、条目全缺依据且无 summary 等）自动放宽上限 3000→6000
  重试一次**，仍失败才 fail-open（返回空结论 + `error` 原因）；有效性判据用**原始载荷**
  （归一化后 dimensions 恒为 5 条会让空 `{}` 也被判有效、吞掉重试）。JSON 前后夹解释文字由
  `_loads` 取首个 `{` 到末个 `}` 的片段提取，不必重试。空文本 / 无 EOP 不调模型，
  直接返回带原因的空结论。
- **周级聚合**（`pipeline`，纯确定性、零额外 LLM 调用）：`severity_counts` 按高/中/低
  计数；`week_summary` 拼接周级统计与各组一句话，如
  「全周共 2 项问题（高 1、中 1）：A班组未发现与 EOP 不符的环节；B班组 2 项问题
  （高 1、中 1）；C班组无法评价（该周没有记录文件）」；
  顶层 `issues` 是各组扁平拼接（每条带 `group`）。
- **缺文件 / OCR 失败 / 无 EOP / 判定失败的班组也各出一行** `groups[]`：`status` 为
  `missing` / `ocr_failed` / `no_eop` / `judge_failed`，问题数 0 并在该行 `error`
  写明原因（这类组只说明「无法评价」，不据此推断其它组的结论）；并发判定上限
  `DRILL_AUDIT_JUDGE_CONCURRENCY`。

## 附件（对话内可交互查看）

`deliver.build_attachments(result, thread_id)` 在工具出口生成两类附件，**每项独立兜底**
（生成失败只记日志，绝不影响审查结论）：

| key | 内容 | 形态 |
|-----|------|------|
| `issues` | 问题明细：序号/班组/涉及环节/问题/EOP 依据/演练记录证据/严重程度/建议 | `<file …/records>` 交互表；≤20 条时另带服务端渲染的 `table_markdown` |
| `records` | 演练过程记录 Markdown（计划信息 + 各组综合评价 + 逐组「演练场景 / 人员分配 / 演练过程时间轴 / 流程图」） | `drill-records-<8hex>.md` 落会话 `workspace/`，`<file>` 在线预览 |

**流程图同时直接贴在对话里**：`build_flows` 用与附件同一个 `analyze_record(raw)['flow']`
给每个有图的班组生成流程图，`attach_flows` 把它写进该组 `groups[].flow`（没图的组不加这个
键）。模型按契约把每个 `flow` 原样输出在该班组评价之后 —— 前端 `MarkdownContent` 识别
```mermaid 围栏块交给 `MermaidRenderer` 本地渲染（可缩放、全屏），用户不用打开附件就能直观
看到演练过程。挂载发生在 `save_week` 之后，**不写进 Mongo 周记录**。飞书 / 微信不渲染
Mermaid，`file_tags.rewrite_for_channel` 把围栏块与 `<mermaid>` 标签一并剥离。

表格行与文字表全部由服务端 `deliver` 生成（`ISSUE_COLUMNS` 固定列序），
模型只原样输出 `file_tag` 与 `table_markdown`，不得增删列或改写列名。
检索命中的 EOP 文档清单不落附件，仅以 `eop_refs`（文档名称 + 相似度）随返回下发，
用户问「依据了哪几份 EOP」时模型口头说明即可。
**不暴露原始 PDF**：扫描件不便阅读，在线查看的是整理后的演练过程记录。
演练过程由 `service/drill_audit/process.py` 把 OCR 文本**确定性**整理为时间轴文字 +
Mermaid **泳道并行流程图**（`flowchart LR` + `subgraph`，前端 `MarkdownContent`→`MermaidRenderer`
本地渲染），**不再转贴 OCR 表格**：相邻重复单元格折叠、卡片正文截掉、按时间戳稳定排序、
**每一步都是一个节点、全量输出不截断**。泳道来自「人员分配」里的班组（`A指挥通报组：…`
`B组(倒闸操作)：…`），指挥泳道在前、其余按首次出现排序；**每条泳道是一个竖排列**
（`subgraph` 内 `direction TB`，根方向 `flowchart LR` 让几条泳道左右并排），列内按时间
串联 —— 并行关系由「一列一条泳道」的版式表达（各列顶部并不对齐）。**刻意不画跨泳道可见
连线**：mermaid 的 dagre 是全局 rank 网格，subgraph 一旦有跨边界的边，内部的 `direction`
会被忽略、几条泳道被拉成一条长链（实测 29 步的记录排出 12 列 rank、「末节点汇到时间最后
一步」那条边穿过 5 个空列，每条泳道的框都被撑满整张图、内容只占一小块）。**但泳道之间要
留一条隐形连线**（`L0 ~~~ L1` …）：完全不相连的竖排 subgraph mermaid 不会并排，而是竖着
叠成一列（实测 4 条泳道全落 x=8、整图 346×5227）；补上后 1486×1939，各道并排且内部的
`direction TB` 仍生效，`~~~` 本身不画线、不承载语义。**人员分配解析不到时**按操作关键词
（供配电/制冷与末端/柴发/现场处置）回退分泳道，仍是多泳道而非一条直线。节点文案去掉说话人
前缀、取第一个短句、超长在字数边界截断到 18 字，时间单独一行（`<br/>`）让列宽只由正文决定；
`#` 换成「号」（Mermaid 里裸 `#` 会被当实体前缀），
`"`/`<>` 等敏感字符剔除。少于 2 步不出图。
OCR 文本经 `store.load_text(file_id)` 读取（内容库命中，零下载）。

## 每周任务与页面（合规检查页第五张卡）

每周一 07:10 自动扫描「到点该审」的演练周并落结论；同一份结论在 `/compliance` 页面的
第五张卡（文档更新 / 事件统计 / 告警不合规 / 红外质检 之后的「应急演练合规审查」）里浏览。
**与对话工具共用同一条流水线与同一份周记录**（`drill_audit_weeks`），只靠 `audit_source`
区分来源（对话 = `dialog`，周任务 = `weekly`），页面两者都展示并标注来源。

### 开关与预设

- `DRILL_AUDIT_ENABLED`（默认 false）—— 总开关；页面（卡片 / 看板 / 手动单周或批量审查 /
  「刷新清单」/ 清单刷新端点）只要求它。
- `DRILL_AUDIT_WEEKLY_ENABLED`（默认 false）—— **只控制每周一 07:10 的自动审查**
  （预设注册条件，需同时总开关为真）；关它**不影响**页面手动触发与计划清单加载
  （2026-11-18 修：此前手动触发也要求它，导致线上手动审查直接 400、清单永远为空）。
- `DRILL_AUDIT_WEEKLY_CRON`（默认 `10 7 * * 1`，周一 07:10，避开报告预设的凌晨槽位）。
- `DRILL_AUDIT_WEEKLY_DEPTS`（默认空 = 复用 `DOC_UPDATE_DEPTS` 的公司 × 部门清单）。
- `DRILL_AUDIT_WEEKLY_LOOKBACK_DAYS`（默认 14）。

预设是独立一族 system 任务（`drill_audit__weekly`，`presets.ensure_drill_audit_presets()`
幂等维护，启动时 `main.py` lifespan 调用）；报告侧的 stale 清理按前缀排除，**互不误停**。
执行走确定性流水线（`weekly.run_weekly_task`，完整 run-record 契约），**不产出附件**
（`artifact_urls=[]`），页面直接读 Mongo 周记录。

### 审哪些周（口径）

- 部门清单：`DRILL_AUDIT_WEEKLY_DEPTS`，为空复用文档更新检查的 `DOC_UPDATE_DEPTS`
  （同样逐公司逐部门；`dept_id` 由 `doc_update.depts` 的注册表缓存解析，不额外调接口）。
- 年份：当年；1 月上旬补上一年（跨年的演练周仍在上一年计划里）。
- 单周入选条件（`weekly.week_candidates`）：`status=0`（已安排）+ `fileStatus` 非空
  （有记录文件）+ `etime` 落在回溯窗口 `[今天−N, 今天]` 内；同一 `week_id` 去重、
  按 `etime` 升序**串行**审查。单部门/单年拉取失败只记 `errors`，不拖垮整轮。
- 幂等口径：周记录 `audited_at`（UTC → Asia/Shanghai）的日期 **≥** 该周 `etime` 当天、
  且没有 `ocr_failed`/`missing` 状态的班组 → 跳过（内容库命中本就零下载零 OCR，
  跳过只为省掉重复判定）；`refresh=true` 绕过内容库重下重解析。

### 端点（`api/v1/scheduled_tasks.py`，权限 `ai:agent`，声明在通用 `/{task_id}` 之前）

| 用途 | 端点 |
|------|------|
| 页面总览（开关 / 部门列 / 月份 / 已审周摘要 / **全部演练计划** / 是否在跑 / 最近运行 / 清单刷新状态） | `GET /scheduled-tasks/drill-audit/overview?period=YYYY-MM` |
| 执行审查（后台异步，抢不到锁返回「已有任务在跑」）：`items` 非空 = 只审这些周；否则全量扫到点该审的周（仅定时任务/脚本用，页面不再暴露） | `POST /scheduled-tasks/drill-audit/run`（body `{items: [{week_id, dept_id?, year?}], dept_names?, refresh?}`） |
| 只刷新计划清单（不下载、不 OCR、不判定） | `POST /scheduled-tasks/drill-audit/roster`（body 可带 `{dept_names}`） |
| 单周详情（整体情况 + 按班组切换：场景 / 人员分配 / 步骤时间轴 / 流程图 / 该组问题） | `GET /scheduled-tasks/drill-audit/weeks/{week_id}` |

`run` 用 Redis 锁 `drill_audit:weekly`、`roster` 用 `drill_audit:roster`（均 TTL 900s）防并发
重复；`overview.running` / `overview.plan_refreshing` 各读同一把锁，`plan_updated_at` 是清单
最近一次刷新时间。总开关关时 `overview` 回 `{'enabled': false, …}` 空壳（页面据此隐藏整张卡）。

### 页面形态（`/compliance` →「应急演练合规审查」卡 → 整屏看板）

- 卡片（`ComplianceTile`）：图标 + 标题 + 周期「每周」+ 一句语境（运行中「正在审查…」/ 已审给
  「已审 N 周」/ 清单刷新中「正在加载计划清单…」/ 空态引导）+ **一排带颜色的小数字指标**
  （全部已审周累计 `severity_counts` 高/中/低，色用 `severityChip(level)`）+ 底栏元信息
  （最近运行/审查时间）；
  **无按钮、无主指标大字、无图表**，「刷新清单」「批量执行」（含单周审查）都只在看板里；
  `enabled=false` 时整张卡不渲染；后台跑着时 3s 轮询（先立即拉一次再起 interval），锁释放即停。
- 看板**列表态是「任务 × 周」矩阵表格**：年份 / 月份 / 部门 / 专业四个下拉筛选（年份与
  月份收窄列、专业只过滤行）；**行 = 全部演练任务**（任务列 = 序号 + 计划名，sticky 左侧；
  紧跟一列「专业」，不 sticky（任务列宽度随内容变化，固定左偏移对不齐）；部门不在表内
  显示，只用于筛选），**列 = 周**（按 `stime` 升序，表头两层：
  上层按月份 `2026 年 10 月` 分组，下层「第 N 周」+ `MM-DD ~ MM-DD`）。列按「年份 + 周号」
  对齐各计划，无周号时退回开始日期。
- **单元格按「已审 / 演练执行状态」四种形态**（`finishStatus` 见 `codes.FINISH_STATUS`：
  1 未开始 / 2 进行中 / 3 已完成 / 4 已逾期）：
  | 情况 | 展示 | 点击 |
  |---|---|---|
  | 有审查记录 | 高/中/低问题 chips（全 0 → 绿色「无问题」），`title` 带来源「对话/每周/手动」、审查时间、完成状态与综合评价摘要 | 进该周详情 |
  | 已排期、已交记录（`file_status` 非空且状态为已完成/已逾期） | 橙色状态 chip（只展示，不直接可点） | 批量模式下勾选（见下） |
  | 已排期但没交记录（`file_status` 为空，或未开始/进行中） | 灰色状态 chip，`title` 说明原因 | 不可点 |
  | 未排期 | 「—」 | 无 |
  「未开始周 `fileStatus` 会预填但无真实文件」—— 可执行判据必须同时要求 `file_status` 非空，
  否则点开必然全是「无记录文件」。
- **单周 / 批量执行（统一走「批量执行」入口）**：待审查的排期格**只展示橙色状态 chip、点它无动作**
  （避免误触直接跑一次下载+OCR+判定的重流程）；筛选条右侧「批量执行」进入勾选模式——排期格与
  已审格都整格点击切换选中（`ring-2 ring-primary` + 勾选图标，不再打开详情），「执行选中 N 周」
  才真正提交（`POST /drill-audit/run` 带 `items:[…]`，后台 `weekly.run_selected_weeks` 逐周调用
  `pipeline.run_audit(audit_source='manual')`，单周失败只记 errors 不中断其余周）。**勾选 1 个即单周
  审查、多个即批量**。切换筛选会清空勾选。**页面的「立即检查」已改为「刷新清单」**（只调 `/drill-audit/roster`，不下载
  不 OCR 不判定）；全量扫描（`run_weekly_audit`）只留给每周一定时任务。
- 看板**详情态**：非卡片页头（计划名 / 专业 / 来源 / 审查时间 / 「重新审查」按钮，第二行
  部门 · 周次 · 时间段 · 完成状态——计划信息已在面包屑与矩阵单元格里，不再重复做成白卡）→
  浅色「整体演练情况」面板（`summary` + `note`）→ **班组胶囊切换**（`groups.length > 1` 时
  显示；胶囊带紧凑状态：非 ok 出状态徽标、ok 出「无问题」或非零严重程度计数；只渲染当前班组：
  状态徽标 + 综合评价 + 演练场景 / 人员分配 + Mermaid 泳道流程图 + 可折叠步骤时间轴）→
  按当前班组过滤的问题明细表（列序同 `deliver.ISSUE_COLUMNS`，标题带「· X 班组」；该组无
  问题出空状态：ok 组「未发现与 EOP 不符的环节」，其他组「未产出问题明细（状态）」）。
  切周时胶囊重置到第一个班组。**审查进行中**详情页顶部出分步进度条（`data.progress`，
  由 `weekly.set_progress` 写 Redis、`pipeline.run_audit` 逐步上报「定位 / 下载并 OCR / 检索 EOP /
  判定 X 班组」），`running` 由 true→false 时**自动重拉该周详情**刷新「审查于」与结果。
  **审查时间按本地时区显示**：后端 Mongo 读回的是 naive UTC（无时区后缀），前端 `fmtTime`
  对其进行「无偏移即按 UTC 解释」归一，避免被当本地时间而慢 8 小时。
- **行/列来源是扫描时落库的计划清单**（Mongo `drill_audit_plans`，一个「部门 × 年 × 计划」
  一条，`weekly.run_weekly_audit` / `weekly.refresh_plan_roster` 拉到计划列表后顺手整批替换
  写入，零额外 HTTP），`overview` 的 `plan_rows` 每行带 `weeks[]`
  （`{key, week_id, finish_status, file_status}`），`plan_columns` 单独发一份列元信息（控制
  3s 轮询载荷）。**列 = 当年全部已排期周（含未到期的未来周）∪ 已有审查记录的周**（跨年记录
  照旧保留），所以从没跑过审查的计划也能看到自己的排期。从未扫描过（集合为空 / 内容库关闭）
  时两个字段都为空数组，前端自动退回「只显示已审行」的旧行为。**旧清单缓存**（行里没有
  `weeks` 或项里没有 `week_id`）会在打开看板时自动触发一次清单刷新，前端另按 24h TTL 刷新。
  **年份/月份筛选只收窄列**：行保留「在可见列里有排期或审查记录」的那些；**专业筛选只过滤行**
  （列不变）。
- **清单自动加载（2026-11-18）**：全量 21 部门共 1035 个计划，一次铺 5.5 万单元格会卡 →
  ① 看板**只能按部门查看**：默认选中第一个部门（廊坊 A1，53 个计划），下拉切其它部门，
  **不提供「全部部门」**（全量 1035 个计划 × 53 周铺满会卡）；
  ② 面板在清单缓存**为空或超过 24h** 时自动后台调一次 `POST /drill-audit/roster`
  （21 部门串行约 30 秒，每会话只触发一次、不重试），期间看板显示「正在加载计划清单…」，
  轮询条件同步扩为 `running || plan_refreshing`。
- **流程图与步骤不落 Mongo**：详情端点读时从内容库 OCR 文本重算
  （`detail.build_week_detail` → `process.analyze_record`），`flow` 去掉 ```mermaid 围栏
  后交给前端 `MermaidRenderer` 渲染；文本已被容量淘汰的组 `steps_available=false`，
  页面回退只展示已存的评价与问题（重跑可补齐）。

## 配置

```dotenv
DRILL_AUDIT_ENABLED=false          # 总开关
DRILL_AUDIT_EOP_GROUP=default      # 检索库（单 collection，真正过滤靠 doc_type）
DRILL_AUDIT_EOP_TOP_K=6
DRILL_AUDIT_LLM_TIMEOUT=120
DRILL_AUDIT_OCR_TIMEOUT=180
DRILL_AUDIT_PARSE_CONCURRENCY=2
DRILL_AUDIT_JUDGE_CONCURRENCY=4            # 并发判定的班组数（上游限流吃紧时可下调）
DRILL_AUDIT_STORE_ENABLED=true              # 内容库开关（关则每轮都重下重解析）
DRILL_AUDIT_STORE_DIR=data/drill_audit      # 内容文件根目录
DRILL_AUDIT_STORE_MAX_MB=2048               # 容量上限，超了按最久未用淘汰

DRILL_AUDIT_WEEKLY_ENABLED=false            # 每周任务总开关（需同时 DRILL_AUDIT_ENABLED=true）
DRILL_AUDIT_WEEKLY_CRON=10 7 * * 1          # 默认周一 07:10
DRILL_AUDIT_WEEKLY_DEPTS=                   # 「公司:部门,…」；空 = 复用 DOC_UPDATE_DEPTS
DRILL_AUDIT_WEEKLY_LOOKBACK_DAYS=14         # etime 在 [今天−N, 今天] 的已安排周才审
```

依赖：`MGMT_*`（取数 + 下载）、Milvus（EOP 检索），以及 OCR 服务二选一 —— 配了
`MINERU_GRADIO_URL` 走本地 Gradio（无需 token）；否则需**有效的 `MINERU_API_TOKEN`**（云端
OCR，过期 JWT 会 401，`.env` 与 `deploy/config/.env.production` 两处都要有）。

## 核对

```bash
# 纯函数自检（29 项，不联网、不连库、不调模型）：码表、周次选择、判定 JSON 解析（含截断重试）、
# 严重程度计数、组行状态、周级拼接、附件行与文字表、OCR 表格折叠、时间轴抽取（含越界秒数进位）、
# 泳道分派/每道一竖列且无跨泳道可见边、泳道间隐形连线 `~~~`/关键词回退/标签清洗与 `#`→号、
# 演练过程记录 Markdown、流程图挂到 groups[].flow（无图组不加键）、检索异常、
# 步骤耗时指标（seq/at/delta、末步 delta=None、timeline_stats）、EOP 标尺高亮抽取、
# EOP §9.3 步骤结构化（同行/分行、字段留空不编造）、维度归一化（补 5 键/无据降级）、
# 漏做步骤镜像进 issues、step_findings 服务端覆盖 actual/越界丢弃/上限截断、
# 旧结构（仅 issues+summary）仍被接受且维度补无法判定
python -m service.drill_audit.tests.test_drill_audit
# 内容库自检（临时目录，不连 Mongo、不联网）：路径分片/原子写读回/字节统计/淘汰顺序
python -m service.drill_audit.tests.test_store
# 每周纯函数自检（6 项，不联网/不连库/不调模型）：日期解析、回溯窗口边界、ISO 跨年周、
# 1 月补上一年、候选过滤+去重+排序、周级汇总文案
python -m service.drill_audit.tests.test_weekly

# dry-run：只定位计划/周/班组并预览 EOP 命中，不下载、不 OCR、不判定
python -m service.drill_audit.cli --dept 23CI1EiZnff --year 2026 --plan-name 制冷

# 完整审查（下载 + OCR + 检索 + 判定）；连跑两次，第二次应命中内容库
python -m service.drill_audit.cli --dept 23CI1EiZnff --year 2026 \
  --plan-name "制冷单元故障+电动阀门故障应急操作演练" --yes

# 绕过内容库重下重解析
python -m service.drill_audit.cli ... --yes --refresh

# 临时压低容量上限验证淘汰（结果仍应正常产出）
python -m service.drill_audit.cli ... --yes --store-max-mb 1

# 只看内容库统计（文件数 / 占用 / 可命中数），不联网
python -m service.drill_audit.cli --store-stats

# 每周任务：只列出本轮会审哪些演练周（不下载、不 OCR、不判定）
python -m service.drill_audit.cli --weekly-dry-run
# 真跑一轮每周审查（可加 --refresh 绕过内容库）
python -m service.drill_audit.cli --weekly --yes
```

计划名子串匹配 0 项/多项都报错并回候选，不猜；`--week` 缺省取最近一个已完成/有记录的
已安排周。

## 边界

- 只有知识库里的 EOP 能当判定依据；文档缺失/OCR 失败都写进 `note`，且该班组在
  `groups[]` 里单独给「无法评价」+ 原因，必须如实转述。
- 每份班组记录各判一次、互不弥补：一个班组的问题不会被另一班组的证据抵消；
  周级 `summary` 只是统计与各组一句话的拼接，具体问题看 `issues[].group` 与 `groups[]`。
- 扫描件 OCR 可能有识别错漏，结论带这层不确定性，不做逐字精确比对承诺。
- `note` 里的「本次复用已解析记录 N 份」是内容库命中，**属正常**，不是故障。
