# 日常变更统计（每日变更通知）

把 6 个园区「日常运维工作」群里**人发的**变更通知，按自然日汇总成「园区 × 数据中心」的变更清单
（变更类型 / 维护事项 / 执行状态），提供对话工具、每日定时与页面三处入口。

- 页面：入口「AI 能力 → 日常运维 → 变更统计」（`/ops/change-notice`，权限 `ai:agent`；「日常运维」`/ops` 为一页两张卡的落地页）
- 对话：`change_notice_report(date/start/end/parks/sync/export_excel)` + 技能 `skills/change_notice/SKILL.md`
- 定时：预设 `change_notice__all__daily`（`CHANGE_NOTICE_ENABLED=true` 才注册），cron `10 18 * * *`
  统计**当天 00:00 ~ 运行时刻**

## 1. 数据来源

- 日常运维群的消息写入**独立表** PG `dcim_change_reports`（读写全走 `change_notice/source.py`；
  DDL 在 `incident_stats/schema.py` 的 `ensure_change_schema`），与应急通报表
  `dcim_incident_reports` **物理分开**：不存 cluster / event_time / match_* 等应急专用列。
  变更记录**读时解析**，不固化快照。
- **入口按群身份路由**（`incident_stats/realtime.py`）：先解析应急园区映射，命中走原应急路径
  （**应急优先**）；未命中且 `feishu.is_change_chat(chat_id)` 则直接写 `dcim_change_reports`，
  不补抽、不触发告警匹配。撤回同理路由到 `change_source.delete_report`。
- 群白名单仍是 `FEISHU_GROUP_INGEST_CHATS`；园区与群的映射单独放 `CHANGE_NOTICE_FEISHU_PARK_CHATS`
  （JSON `{园区: chat_id}`，解析单一来源 `feishu.change_park_chats()`），**绝不能写进
  `INCIDENT_FEISHU_PARK_CHATS`** —— 日常群不是应急通报群。
- **只统计人发送的消息**：机器人消息**不入库**——WS 实时按 `sender_type=app`（或只有 `app_id`）命中即丢、
  回补按 `sender.id_type=app_id` 过滤；读侧仍要求 `sender_id_type == 'open_id'` 作纵深防御。
  共享部门账号同样是 `open_id`，符合「人发送」口径。
- 分类上 `feishu.classify(text, chat_id)` **先按群身份**判：日常群只产出 `change`（命中变更头）/
  `other`，**绝不产出 `sos`/`first`**（纵深防御：即使误送应急写库也不会被当成待匹配通报）。
- 历史遗留（分表前误写进应急表）的行用 `deploy/scripts/split_change_reports.py` 搬迁：默认 dry-run，
  按 `is_change_chat()` 的 chat_id 集合 `INSERT ... SELECT` 后逐批 `DELETE`，`--yes` 才真执行。

## 2. 三种纳入统计的报文格式（实测四种形态）

**统计纳入 `lf`（廊坊式）、`bracket`（佛山/重庆方括号式）与 `ph`（平湖AB 编号列表维护通告）**；
`ph` 无工单号，其「位置」是楼宇字母（Y楼/Z楼/W），按 `parse._DC_ALIASES` 映射成数据中心代号。
`repair`（惠州维修变更，无计划时间）**不算正常变更，`parse_message` 直接丢弃**
（`parse._ENABLED_FORMATS`，解析代码与正则保留以便回退）。

| 园区 | 开始 / 结束标记 | 字段形态 | 工单号 | 统计 |
|------|----------------|----------|--------|------|
| 廊坊 | `【变更开始-通知】` / `【变更完成-通知】` | 竖线分隔 `标签：值` | `RZKJ-LF-…` | 纳入（lf） |
| 平湖AB | 两套并行：廊坊式 `lf` + `【…维护通告】开启` 编号列表（ph） | 竖线 `标签：值` / `1、标题 2、位置 …` | lf 有，ph 无 | 纳入（lf + ph） |
| 佛山 / 重庆 | `【变更开始-<标题>】` / `【变更结束-<标题>】`（可能被 `<p>` 包裹） | 方括号 `【变更单号】值` | `RZKJ-FS-…` / `RZKJ-CQ-…` | 纳入（bracket） |
| 惠州 | `【维修变更开始】<标题>` / `【维修变更完成】<标题>`（标题在抬头同行） | 方括号，工单号在 `【故障维修单号】` | — | 不纳入 |

- 平湖AB 的 ph「位置」是楼宇字母，按 `parse._DC_ALIASES` 映射数据中心：**Y楼 → A1、Z楼 → A2、
  W（W楼）→ M3M4**；工单代号 / `X数据中心` 两条路径在前，不受影响。
- ph 与 lf **不是重复、不做跨格式去重**（用户口径）：同一件事两种模板各发一遍时**各自成行**；
  ph 无工单号，开始/完成按「园区 + 数据中心 + 标题 + 日期」配对成一条（`build._key` 的 adhoc 回退路径）。
- 佛山曾有 `佛山-A1`（共享部门账号，open_id）发的方括号变更，但都在 **10-29 / 11-13 / 11-17**，
  早于统计起点 2026-11-19；起点之后佛山群里只有机器人卡片（`app_id`）与值班名单，暂无纳入统计的报文。
- 解析前做 NFKC、`<br>`/`<p>` 剥标签、括号归一；**不复用会改写【】的 `_norm`**。
- 噪声消息（值班名单、天气、@_all 说明、110 变电站值班）不含变更头，自然丢弃。
- 时间解析认 `YYYY年M月D日H:MM` 与横杠/斜杠写法；计划时间是区间时拆成开始/结束。

### 2.1 字段抽取兜底（未登记标签交给模型）

现场报文写法一直在变。实测 2026-11-19 廊坊 53 条里 5 条（≈9%）带未登记标签
（`实际开始时间 / 实际结束时间 / 完成时间 / 变更完成描述`）。**不再逐个人工补别名**，
改为确定性解析 + 模型兜底：

- `parse._classify_line` 对**未登记但命中语义提示词**的 `标签：值` 记为 `unknown`
  （方括号/编号/普通三种形态都认），**不再并入上一条字段**（原先会污染「现场负责人」）；
  无提示词的续行（如影响范围里的 `5F-3#电力室:`）仍按续行处理。
- `parse_message` 在事件 dict 上附带内部键 `unknown`（标签名列表）与 `_text`（归一原文），
  只供兜底；`build` 只读声明字段，两键不进任何输出列。
- **负责人两档、只留姓名**（2026-11-23）：负责人相关标签分「负责人性质」（`现场负责人 /
  变更负责人 / 相关负责人 / 现场联系人 / 人员` → `contact`）与「值班兜底」（`值班工程师 /
  值班人员` → `contact_fallback`）两档，同档多标签**累加**而非首见即定；出口取
  `contact_names(第一档) or contact_names(第二档)`——**负责人优先、值班兜底**。`contact_names`
  按分词切分后剥掉标签前缀、电话/工号（只留 2~8 个汉字姓名），合并「秦   昊」这类大字间距、
  保序去重。例：重庆 `值班电话：023-… / 值班工程师：朱传佳、秦昊 / 变更负责人：朱传佳`
  → 现场负责人只取 `朱传佳`（值班工程师里的 `秦昊` 不入列）。
- 调模型（`change_notice/extract.py`）的闸放宽为：`unknown` 非空，**或**「负责人列为空但原文
  出现负责人相关标签」（`负责人 / 工程师 / 联系人 / 值班人员 / 姓名`）——后者兜住「标签都登记了、
  但值里只有电话没有姓名」的情况（如上面重庆那句里只发 `值班电话`）。只从上文摘录、禁止推测，
  输出严格 JSON；**只填空字段**（确定性已解析出的值一律不覆盖）；时间必须过
  `parse.parse_dt` 且落在消息时间附近（计划 ±60 天、实际 ±7 天），越界丢弃；模型给的
  负责人也要再过一遍 `contact_names`（仍只接受能归一成姓名的值，绝不写回电话串）。
- 结果按 `sha1(fmt|msg_time|_text)` 缓存 Redis（`change:extract:`，30 天），页面/对话/定时
  共用同一份缓存；传输/解析失败不缓存、下一轮再试，异常绝不影响出表。
- 配置：`CHANGE_NOTICE_EXTRACT_LLM_ENABLED`（默认开）/`_MAX_PER_ROUND`（默认 20）/
  `_TIMEOUT`（默认 15s）。四入口统一走 `workflow.collect_events()`（异步）。

## 3. 统计口径

- **自然日**（`Asia/Shanghai`），按消息 `create_time` 的日期归属；缺省 = 今天
  （当天 00:00 ~ 当前时刻；历史日期仍是整天）。`load_messages(start_s, end_s, chat_ids=…)` 左闭右开。
- **配对键**：有工单号 → `(园区, 工单号)`；无工单号 → `(园区, 数据中心, 标题, 日期)`
  （历史 ph 格式的回退路径，当前纳入格式都带工单号）。
- **一个工单可对应多次变更**（2026-11-24 A18 实测：同一单跨班次做两轮、窗口与负责人均不同），
  故同一配对键内再按**变更窗口**（计划开始，缺失时用实际开始；归一到分钟）分条：
  - 开始/取消通知：窗口相同 → 并入同一条（重发去重），窗口不同 → 各自成条；
  - 完成通知：先配窗口一致的开始那条，配不上再并入该工单最近一条未完成的通知。
- **合并口径**：文本取首个非空、计划/实际时间取最早、状态取最高（已完成 > 已取消 > 进行中），
  消息时间取最早。
- **执行状态**：见到完成/结束 → 已完成；含「取消」→ 已取消；只有开始 → 进行中。
- **变更类型**（由变更等级分桶，**无依据不推断**）：`紧急/临时` → 临时新增紧急变更；
  `高/高风险/高级` → 高风险变更；`中/中级/中风险` → 中风险变更；无等级 → 未标注。变更等级单列保留。
- **现场负责人**只放**姓名**：按「负责人优先、值班兜底」取（详见 §2.1），电话/工号一律剥掉；
  多名以 `、` 拼接。解析不出姓名时留空（可交模型兜底），不拿电话串顶替。

## 4. 明细列（14 列）

园区 / 数据中心 / 变更类型 / 维护事项 / 执行状态 / 计划开始 / 计划结束 / 实际开始 / 实际结束 /
工单号 / 变更等级 / 现场负责人 / 现场负责人等级 / 消息时间。

概览接口另给每园区 ×（各类型计数）+（各状态计数）。

### 现场负责人等级（2026-11-20）

报文里只有负责人**姓名**，职级只在综合管理平台的人员档案里 → 出表链路补一次查询，结果写进
`现场负责人等级` 列。实现在 `service/scheduled_task/change_notice/people.py`，在
`workflow.collect_events()` 里、**配对去重之后**执行（`contact` 已合并成最终值，同一工单不重复查人）。

- **两级接口**：先 `GET /user/info/v2/{姓名}`（按姓名模糊查人，只用来拿 `uid` / `deptId`，只保留**姓名精确相等**的候选），
  再 `GET /user/dept/v4/page?deptId=…&name=…` 取 `users[].level`（**这才是职级**，实测「初级 / 中级 / 无」）。
  **不用 `professionLevel`** —— 那个 1/2/3 实测与职级口径不一致（同一人 `professionLevel=2` 但档案 `level=中级`、
  甚至 `=初级`），不能当职级。
- **重名歧义**：多个候选等级冲突时，用事件所在园区名去匹配候选部门名（`deptName` 含「廊坊」等）；
  仍分不出就**留空不猜**（不配优于配错）。
- **缓存**：按姓名缓存候选人列表（等级命中 TTL 7 天 `CHANGE_NOTICE_CONTACT_LEVEL_TTL`、查无此人固定 1 天自愈）；
  任一步调用失败本轮不写缓存，下轮重试。
- **fail-open**：接口失败/超时/解析异常一律留空，**绝不影响出表**；整批并发有 8s 单人 / 20s 整体超时。
- **「无评级」**：仅当档案确实有此人、但其 `level=无/空` 时显示「无评级」；查不到人/歧义/失败仍是空串。
- 开关 `CHANGE_NOTICE_CONTACT_LEVEL_ENABLED`（默认开）与 `MGMT_ENABLED`，任一为假则该列恒为空、不发外部请求。

### 等级匹配（页面，2026-11-20）

核对**现场负责人等级是否满足变更等级要求**，由 `build.match_of(type, contact_level)` 判定。

| 变更类型 | 最低要求职级 |
|----------|--------------|
| 低风险变更 | 初级及以上 |
| 中风险变更 / 高风险变更 / 临时新增紧急变更 | 中级及以上 |
| 未标注 | 无法判定（无依据不推断） |

- 高级不再作为任何变更类型的硬门槛（2026-11-23 口径调整：中级即可做中/高风险与紧急变更）。
- 人员等级为空、「无评级」、档案查不到（含接口失败）→ **无法判定**，不算「不符合」。
- 一条变更写了多名负责人（`contact_level` 以 `、` 拼接）→ **取最高职级**，任一人达标即「符合」。
- 只展示在页面：`overview()` 每园区多给 `by_match`（符合/不符合/无法判定计数）；records 端点用
  `to_rows(events, with_match=True)` 判定后，按 `build.PAGE_COLUMNS` 返回 **15 列**（「等级匹配」紧跟
  「现场负责人等级」），页面把它渲染成配色胶囊。
  **`build.COLUMNS` 仍是 14 列**，对话 `table_markdown`、xlsx、定时产物不受影响（页面「导出 Excel」
  走 `xlsx.to_bytes(..., page=True)`，同样 15 列并给重点列填底色）。

## 5. 三处入口

- **页面**（2026-11-20 重做）：顶部 Hero 卡（总条数大数字 + 三项等级匹配小计）、日期 + 园区筛选、
  变更类型 / 执行状态 / 等级匹配三张分布卡、`RecordsTable` 明细（「变更类型」「执行状态」「等级匹配」
  三列均渲染为配色胶囊，0 行出空状态）。
  `GET /scheduled-tasks/change-notices/overview?date=` 与
  `GET /scheduled-tasks/change-notices/records?date=&park=`（后者直接回 `{columns, records}`，
  不套 `{ok,data}`）。园区群未配置时回空数据 + `note`，不让页面 500。
  **园区下拉是前端固定列表**（廊坊 / 平湖AB / 佛山 / 惠州 / 重庆 / 巴淡，与 `CHANGE_NOTICE_FEISHU_PARK_CHATS`
  已配园区一致），不依赖接口、也不看当天有没有数据；新增园区需同时改配置与该常量。
  **导出 Excel**（筛选栏「导出 Excel」）：`GET /scheduled-tasks/change-notices/export?date=&park=`
  按当前日期 + 园区直接回字节流（不落盘），产物两张 sheet —— 明细 **15 列**（在「现场负责人等级」
  之后多一列「等级匹配」）+ 统计概览；**变更类型 / 执行状态 / 等级匹配三列按值填底色**
  （与页面胶囊同一套色）。定时预设 / 对话 / CLI 的 xlsx 仍走默认路径：14 列、无底色。
  导出与页面共用 `collect_events` 结果缓存；园区群未配置时回 400 带原因，不产出误导性空文件。
- **对话**：`change_notice_report` 交付照 `incident_report` —— ≤20 行服务端 `table_markdown` +
  `<file …/records>` 交互表格；`export_excel=true` 才写 xlsx 到会话 `workspace/`。
  `sync=True` 先按窗口回补群消息，失败只带 `sync_error` 继续。
- **定时**：`workflow.run_change_notice_task(task)` 跑当天（18:10 触发，窗口 = 当天 00:00 ~ 运行时刻）
  → xlsx 存数据集 → `finish_run`。**18:10 之后才发的变更不进任何一期**（次日那轮从次日 00:00 起算）。
  **0 行不算失败**：照出空表、状态 SUCCESS、通知写「本日无变更记录」。取不到数据/解析全失败也
  必须 `finish_run`（异常落 FAILED），否则页面永远卡「生成中」。

## 5.1 页面打开为什么快（结果缓存 / 单飞 / 并发）

- **结果缓存按数据水位为键**：`workflow.collect_events` 先查窗口内 `(count(*), max(id))`
  （`source.message_stats`），键为 `change:events:{日期}:{园区集合}:{count}:{max_id}`。
  **数据没变 → 同一键 → 直接复用上次的事件列表**；群里发了新消息、回补补上历史、
  撤回删了行，都会让键变化 → 下一次打开必重算一次。键里带**日期**而不是窗口结束时间
  （今天的结束时间每秒在走，带进去就永远命中不了）。TTL 由
  `CHANGE_NOTICE_PAGE_CACHE_SECONDS`（默认 120s，0 = 关闭）控制，只负责回收，
  新鲜度由水位保证。
- **跨 Redis TTL 的持久化（Mongo 快照）**：Redis 缓存只有 120s，过期后（或换日期换了键）
  先读 Mongo 集合 `change_notice_snapshots`（`_id={日期}|{园区集合}`，存 `count/max_id/events`
  + `SNAPSHOT_VERSION`）。水位与版本都对得上就直接复用 —— **历史日期的数据不再变，
  所以第二次起秒开**，不再走模型抽取与人员档案查询；对不上才重算，算完写回 Redis + Mongo。
  时间字段走 `{'__dt__': iso}` 编解码（**不存原生 BSON datetime**，否则读出是 naive UTC、
  北京时间早 8 小时）。Mongo 读写各限 3s、失败一律 fail-open 照常计算。
- **单飞（同一时刻两个请求只算一次）**：页面会并发发 `overview` + `records` 两个请求。
  未命中缓存时先抢 Redis 锁 `change:events:lock:{key}`（`SET NX EX`）：抢到的算一次、
  写缓存；没抢到的每 0.5s 轮询缓存（最多约 30s），拿到就返回、等不到才自己算。
  释放用 Lua 比对 token，绝不误删别人的锁。
- **明细与汇总共用同一份计算**：`records` 端点也按**全园区**计算，再在内存里按园区过滤，
  于是它与 `overview` 用同一个缓存键；选园区不再各算一遍。
- **建表 DDL 进程内只跑一次**：`ensure_change_schema` 原先每个请求都执行
  `CREATE TABLE/INDEX`（两个并发请求还会等锁），现由模块级 `_schema_ready` 记忆。
- **抽取改有界并发**：`extract.enrich` 仍是「每轮最多 N 条」，但由串行改为
  `asyncio.gather` + `Semaphore`（`CHANGE_NOTICE_EXTRACT_LLM_CONCURRENCY`，默认 4），
  5 条未登记标签的报文从最坏 75s 压到约 2 轮 × 15s。单条失败仍只记日志、不影响出表。
- **取消防抖型重复**：外部接口（模型 / 人员档案）短暂失败的结果会随事件列表一起进缓存，
  而不是每次打开页面都重新打一遍上游 —— 这是有意的取舍。代价：历史日期一旦进了 Mongo 快照，
  该日就不再重试（要等数据水位变化或调 `SNAPSHOT_VERSION` 才重算）；当天则因新消息让水位变化而自然重试。
- 概览/明细两个请求的前端超时放到 60s（冷算偶尔仍接近 30s）。Redis 不可用、锁拿不到、
  水位查询失败：一律退回「照常计算」，不影响出表。

## 6. 边界

- **数据从 2026-11-19 起**：人是这天开始按新流程发的，此前的日期只会是空表（不报错）。
- **惠州当前恒为 0**（维修格式不纳入）；**佛山**起点后暂无方括号报文（历史 8 条在 11-17 之前），
  巴淡 / 平湖D 消息量少或群未配置，也可能整日无变更。
- 只解析**已知四种形态、纳入其中 `lf`/`bracket`/`ph` 三种**（ph 的楼宇字母按 `_DC_ALIASES` 映射）；
  新园区接入或报文改版需在 `parse.py` 补正则与用例。未登记但带语义提示词的字段由模型兜底（见 §2.1），
  无提示词的新写法仍需补别名。
- 不处理机器人的卡片式通知（含机器人改写的变更汇总），口径就是「人发的才有」。
- **园区级变更的数据中心列留空**：如 `RZKJ-LF-JSBZB-ZNH-…`（变更地点「廊坊园区动环监控系统云平台」，
  影响 A1~A18 多个楼宇）没有数据中心代号，退不回单个楼宇 —— 不按影响范围猜。
- **综合管理平台的自动卡片（`app_id`）不在采集范围**：抬头 `【A5分部】-变更通知-变更开始 /
  -变更进展开始 / -变更进展结束`，纯文本正文只有「请升级至最新版本客户端，以查看内容」占位符。
  群公告（2026-11-19）说明人工继续手发用于并行核对，若自动卡片无误 **2027 年起不再手发**；
  届时需改为解析卡片 JSON（`im.v1.message.get` + card content），并同步调整「只统计人发」口径。

## 7. 变更通知合规周报（分析报告 · 每周）

与每日统计**同源取数**（`workflow.collect_events`），但产出一份可直接下载的 PDF，放在「分析报告」页的
预设 `changenotice__nationwide__weekly`（全园区合并成一张表）。走**专用确定性工作流**
`change_notice/weekly_report.py`（`runner.run_task` 按前缀 `changenotice__` 早分支，不建 agent、不调 LLM）。
**注意类型 id 是 `changenotice`（无下划线）**，与每日统计族 `change_notice__` 刻意区分、互不吞并。

PDF 结构：统计概览（KPI 卡）→ 两张图（各园区合规情况 / 各规则违规数）→ 合规率汇总（规则表 + 园区表）
→ 附录：不合规明细清单。

### 7.1 可扩展规则表（唯一扩展点）

合规判定是 `change_notice/compliance.py` 里的 `RULES` 元组，**加规则只往 `RULES` 追加一项**
（`Rule(key, title, fn)`，`fn(event, cfg) -> (Verdict, detail)`，`Verdict` 为 `True` 合规 / `False` 不合规 /
`None` 无法判定）。当前三条：

| key | 标题 | 判据 |
|-----|------|------|
| `late_start_notice` | 发送时间晚于计划开始时间 | 有 `planned_start` 且有 `start_msg_time` 时，差值 > 宽限分钟 → 不合规；缺任一项 → 无法判定 |
| `missing_start_notice` | 当日未发送变更开始 | `has_start` 为假 → 不合规（恒可判定） |
| `completion_time_mismatch` | 完成通知时间与实际完成时间不符 | `\|end_msg_time − actual_end\|` > 阈值分钟 → 不合规；通知没写实际完成时间 → 无法判定 |

阈值来自配置：`CHANGE_COMPLIANCE_COMPLETION_DIFF_MINUTES`（默认 **30**）、
`CHANGE_COMPLIANCE_LATE_START_GRACE_MINUTES`（默认 0）。**无法判定不计入不合规、不臆造。**

`build._merge` 产出 4 个内部键供判定用（不进任何输出列）：`has_start` / `start_msg_time` /
`has_end` / `end_msg_time`（同工单「开始 + 完成」两条消息聚成一行后，按 `status` 反推谁是开始通知、
谁是完成通知，取各自最早一次；无需改 `parse.py`）。

### 7.2 口径

- **合规率（事件级）** ＝ 无任何不合规规则的事件数 / 总事件数；`0` 事件时合规率为 `None`（报告显示「—」）。
  另给每规则各自合规率（合规 /（合规 + 违规），无法判定不入分母）。
- **窗口**＝上一个完整自然周（`report_templates.window_for('weekly')`，周一~周日，`Asia/Shanghai`）。
  **带回看** `CHANGE_COMPLIANCE_LOOKBACK_DAYS`（默认 7）：`_merge` 按 `(园区, 工单号)` 把「开始 + 完成」
  配成一行，跨周时「上周开始、本周完成」的开始通知落在回看区，不带回看会把规则①②误判成「没发开始」。
  回看只为补齐开始通知，**事件归属仍按本周时刻**（`_attribution_ts`：完成通知 > 开始通知 > 消息时间）过滤。
- **0 事件不算失败**：落一条 SUCCESS、`artifact_urls=[]`，页面显示「无产物」。任何异常都 `finish_run(FAILED)`。

### 7.3 自检

```bash
python -m service.scheduled_task.change_notice.tests.test_compliance   # 规则边界/无法判定/合规率/窗口/扩展
python -m service.scheduled_task.change_notice.tests.test_change_notice # _merge 内部键
```
