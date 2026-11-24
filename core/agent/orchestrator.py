"""编排层 —— 多标签编排的 Step 2（工具路由）。

拿意图识别器（query_planner）产出的 IntentPlan，按 intent_labels 决定预 promote
哪些工具组，产出 agent 的 initial_state。核心思想：路由到具体工具靠**查绑定表**
（deterministic），不靠语义搜索；LLM 后续只负责填参数。命中标签组**全部**预 promote
（不按查询类型截断）；真正的多轮/多工具执行由 agent 在 ReAct 循环里自决。

双重保险的代码层：intent_labels 先过滤（只保留绑定表里真实存在的），过滤后为空
则返回 None → 不预 promote → 走 tool_search fallback。因 Literal 约束模型本不会
输出非法标签，此处过滤是防御性兜底（含词汇表未定/模型异常等边界）。
"""

import logging
import re

from core.agent.query_planner import IntentPlan

logger = logging.getLogger(__name__)

# 这些 task_mode 下才放开「模式门控工具」（见 service/query/base/platform.py 的
# MODE_GATED_PLATFORMS）。research = 前端「联网调研」模式。
# 另有前端「联网搜索」开关，走同一放开逻辑但**不注入调研方法论**（见 build_initial_state）。
MODE_GATED_TOOL_MODES: frozenset[str] = frozenset({'research'})


def route_to_tools(plan: IntentPlan, catalog_hash: str) -> dict | None:
  """按 IntentPlan 查绑定表，产出预 promote 的 PromotedTools。

  Args:
    plan: 意图识别结果。
    catalog_hash: 当前 deferred catalog 的指纹，promote 需按此隔离。

  Returns:
    PromotedTools 形态 dict {'catalog_hash', 'names'}；无可 promote 时返回 None
    （调用方据此走 tool_search fallback）。
  """
  from service.query.base.intent_bindings import build_intent_bindings
  from service.query.base.platform import platform_of
  from service.query.base.tools_factory import capability_to_tool_name

  bindings = build_intent_bindings()
  if not bindings:
    return None  # 词汇表未定义，无绑定可查

  # 代码层过滤：只保留绑定表里真实存在的标签（防御性兜底）
  valid_labels = [l for l in plan.intent_labels if l in bindings]
  if not valid_labels:
    return None  # 空标签 → tool_search fallback

  # 聚合命中能力 id（去重保序）——命中的标签组全部预 promote，不做截断
  cap_ids: list[str] = []
  for label in valid_labels:
    cap_ids.extend(bindings[label])
  cap_ids = list(dict.fromkeys(cap_ids))

  # ★ 平台过滤：“先选平台”。类目词汇是**跨平台**的（`device` 同时装着综合管理平台与
  #   DCIM 的设备能力），不过滤就会把两个平台的同类工具**一起塞给模型**，让它自己猜 ——
  #   而 planner 已经判过 domains 了，这里就该用上（此前 plan.domains 是**产出即弃**的）。
  #
  #   注意：**不能反向做**（domain 命中就整平台 promote）——那会把该平台 51 条 mgmt 工具
  #   全塞进去。只用来**收窄**已由标签选出的能力，不做扩张。
  #
  #   过滤后为空 → 返回 None → 走 tool_search fallback（既有兜底，不会硬塞脏数据）。
  if plan.domains:
    filled = [c for c in cap_ids if platform_of(c) in plan.domains]
    if filled:
      cap_ids = filled

  if not cap_ids:
    return None

  names = [capability_to_tool_name(cid) for cid in cap_ids]
  logger.info(
    'route_to_tools labels=%s → promote %d 工具',
    valid_labels, len(names),
  )
  return {'catalog_hash': catalog_hash, 'names': names}


# 关键词 → category 标签（绑定词汇，见 intent_bindings.py 回退 category）。
# 覆盖口语简写（故障单/变更单/阈值单等）；过度命中安全——正则只走「恰好 1 类」的
# 快路径，命中错的最差 = 主 LLM tool_search 兜底（不劣于无 plan）。
_CATEGORY_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
  # 工单类（work_order）：各类型「工单/单/裸词」全收
  (('待办工单', '我的工单', '我的待办', '我的已办', '待办', '已办', '已发起', '抄送',
    '变更工单', '变更单', '变更', '故障工单', '故障单', '故障', '问题工单', '问题单',
    '事件工单', '事件单', '事件', '阈值工单', '阈值单', '阈值', '供应商', '工单'), 'work_order'),
  # 组织人事（org，含培训/人员/部门）
  (('人员', '员工', '部门', '分部', '负责人', '培训', '名单', '档案', '职位', '工号',
    '我的信息', '我的部门'), 'org'),
  (('巡检',), 'inspection'),
  # 演练：演练计划/周安排查询（与内置审查工具 drill_compliance_audit 分工不同）
  (('演练', '应急演练', '演练计划'), 'drill'),
  (('设备台账', '设备', '台账'), 'device'),
  (('维护计划', '维护', '保养', '维修'), 'maintenance'),
  # 注意**没有「温度」**：它原是 mgmt 告警的口语兜底，但会让「温度曲线」**恰好命中 1 类**
  # → 快路径 promote 一堆 mgmt 告警工具，**根本到不了 planner**，平台选择就无从谈起。
  # 去掉后：「温度告警」仍命中「告警」→ alarm ✅；「温度曲线」→ 0 类 → 落 planner，
  # 由 LLM 按平台定位选 DCIM ✅。
  (('告警', '报警', '告警工单'), 'alarm'),
  # PUE / 能耗：**有意不登记裸词「用电」** —— 它会与「容量/机柜」等叠加，
  # 破坏「恰好 1 类才走快路径」，让本该直连的查询落到 planner 白付一次 LLM。
  (('PUE', 'pue', '能耗', '耗电量', '用电量'), 'energy'),
  (('容量', '机柜'), 'capacity'),
  # DCIM 的空间维度（绑到 asset）。**有意与「设备」分开**：像「某机房有哪些设备」
  # 会同时命中 asset + device 两类 → 不满足「恰好 1 类」→ 落 LLM planner 兜底，
  # 由它决定 promote 哪几组（比硬猜一类更稳）。
  (('资产', '空间', '机房', '园区', '楼宇'), 'asset'),
  (('文档', '制度', 'SOP', '知识库', '标准', '规范'), 'document'),
  (('评审',), 'review'),
  (('配置',), 'config'),
]


def regex_route(message: str) -> list[str]:
  """正则快路径：扫消息，返回命中的 category 标签列表。

  命中多个返回多个（供调用方判断：仅恰好 1 类才走快路径）；无命中返回空。
  """
  matched: list[str] = []
  for keywords, label in _CATEGORY_KEYWORDS:
    if any(k in message for k in keywords):
      matched.append(label)
  return matched


# 常用查询 → 精准能力表：命中即只 promote 这条查询真正要用的几个工具（含部门/公司解析），
# 替代“命中类别就整组 promote”。本轮只覆盖工单相关 7 组。
_PROMOTE_MAX = 12
_RESOLVERS = ['mgmt.query_pen_company_dept', 'mgmt.query_company_list', 'mgmt.query_my_info']
_QUERY_TOOLS: list[tuple[tuple[str, ...], list[str]]] = [
  # PUE / 能耗是 dcim_meta 专有说法（mgmt 没有能耗数据），不会抢走别的平台的查询。
  # 与类别词同口径：**不登记裸词「用电」**。
  (('PUE', 'pue', '能耗', '耗电量', '用电量'), ['dcim_meta.query_pue']),
  # 动环告警工单是 dcim_meta 专有说法，mgmt 的工单分类里没有它。
  # 不加这条时「告警工单」同时命中 work_order + alarm 两个类别 → 落 LLM planner，
  # 实测 planner 按「工单」把平台判成 mgmt → dcim 告警能力被 domains 过滤，
  # 模型只能反问「故障单还是事件单」（2026-10-20）。
  (('告警工单', '告警处置', '告警处理', '响应超时', '响应超过'),
   ['dcim_meta.query_alarm_orders', 'dcim_meta.query_alarms']),
  (('变更单', '变更工单', '变更'),
   ['mgmt.query_change_ticket', 'mgmt.query_change_ticket_detail', *_RESOLVERS]),
  (('事件单', '事件工单', '事件'),
   ['mgmt.query_event_ticket', 'mgmt.query_event_ticket_detail', *_RESOLVERS]),
  (('故障单', '故障工单', '故障'),
   ['mgmt.query_breakdown_ticket', 'mgmt.query_breakdown_ticket_detail', *_RESOLVERS]),
  (('问题单', '问题工单', '问题'),
   ['mgmt.query_question_ticket', 'mgmt.query_question_ticket_detail', *_RESOLVERS]),
  (('待办', '我的工单', '我的待办', '已办', '已发起', '抄送'),
   ['mgmt.query_pending_ticket', 'mgmt.query_my_info']),
  (('阈值单', '阈值工单', '阈值'),
   ['mgmt.query_threshold_ticket', 'mgmt.query_threshold_ticket_detail', *_RESOLVERS]),
  # 演练：计划/周明细 + 公司/部门解析工具。A1/A18 这类名称在多家公司同名（A1分部在润泽/
  # 海外/平湖/佛山/重庆 5 家都有），必须先定公司 —— 故同轮 promote 公司/部门工具；
  # 去掉 query_dept_by_name（它只返回同名部门中的一条，实测 A1 解析到海外园区且该部门无计划）。
  (('演练', '应急演练', '演练计划'),
   ['mgmt.query_drill_plans', 'mgmt.query_drill_week',
    'mgmt.query_pen_company_dept', 'mgmt.query_company_list', 'mgmt.query_dept_list']),
  # 口语里常说「服务工单」而非「供应商工单」：只登记后者会让精准表 miss → 落到类别
  # 正则（「服务工单」含「工单」+「例行维护」含「维护」= 命中 2 类，不满足「恰好 1 类」）
  # → 走 LLM planner 兜底，白付一次 planner LLM 且整组 promote 14 个工具
  # （21853 字符 schema ≈ 13.7k token）。2026-09-28 实测。
  (('供应商工单', '供应商', '服务工单'),
   ['mgmt.query_supplier_ticket', 'mgmt.query_supplier_ticket_detail', *_RESOLVERS]),
  # 注：这里**有意不登记 DCIM 的词**（曾加过「曲线/时序/趋势/测点/点位」）。
  # 「曲线/趋势/时序」是**跨平台**的：综合管理平台做数据分析时也会用（"变更趋势"），
  # 精准表命中就会**替 LLM 把意图判给 DCIM**、把 mgmt 的工具挤掉。
  # DCIM 的归属改由 **planner 的平台定位判断**（见 `route_to_tools` 的 domains 过滤 +
  # `manifest._PLATFORM_DEFINITIONS`）—— 那里有上下文、能按语义判，比关键词表准。
  # 例外：「告警工单」（见本表首条）不跨平台 —— mgmt 的工单分类里没有它，
  # 登记它不会抢走 mgmt 的任何查询。
]

# 统计类查询 → 精准能力表（**先于上面那张明细表判断**，见 match_query_caps 的说明）。
# 为什么必须先判：「变更数据统计」里含「变更」，会被明细表的 ('变更单','变更工单','变更') 命中 →
# Step1 只 promote 了列表能力，统计能力**根本没进模型工具列表**（2026-09-18 实测：模型因此只能走
# "拉列表 + 沙箱自己算" 那条路）。这里先判统计说法，命中就把明细表让开。
_STAT_QUERY_TOOLS: list[tuple[tuple[str, ...], list[str]]] = [
  (('变更统计', '变更数据统计', '变更统计表'),
   ['mgmt.query_change_statistics', *_RESOLVERS]),
]


def match_query_caps(message: str) -> list[str]:
  """扫常用表，返回命中能力 id（有序去重并集；多命中=多意图，均保留）。

  全局上限 _PROMOTE_MAX：超限优先保留各命中条目的“主查询工具”，再补 detail/解析类。

  **统计类优先**：命中 `_STAT_QUERY_TOOLS` 时只返回统计能力，**不再并入下面的明细表**——
  否则「变更数据统计」里的「变更」会命中明细表，Step1 把列表工具 promote 给模型，
  统计能力反而进不了工具列表（2026-09-18 实测）。
  """
  stat_entries = [(kws, caps) for kws, caps in _STAT_QUERY_TOOLS if any(k in message for k in kws)]
  if stat_entries:
    stat_out: list[str] = []
    for _, caps in stat_entries:
      for c in caps:
        if c not in stat_out:
          stat_out.append(c)
    return stat_out[:_PROMOTE_MAX]
  hit_entries = [(kws, caps) for kws, caps in _QUERY_TOOLS if any(k in message for k in kws)]
  if not hit_entries:
    return []
  out: list[str] = []
  for _, caps in hit_entries:
    for c in caps:
      if c not in out:
        out.append(c)
  if len(out) > _PROMOTE_MAX:
    mains = [caps[0] for _, caps in hit_entries]
    out = ([c for c in mains if c in out] + [c for c in out if c not in mains])[:_PROMOTE_MAX]
  return out


# 内置扩展桶 → 工具名的正则预 promote。**只登记工具名，绝不登记技能**：`skill__*`
# （skill__incident_report 等）本来就走技能入口、常驻可见，不参与这张表；技能管理类
# 说法也不在这里（skill_manage 由 <extension-tools> 扩展段 + tool_search 承担）。
#
# 命中即**独占返回**（与 _STAT_QUERY_TOOLS 同语义）：这些是确定性流水线，混入平台
# 工具只会干扰；缺的另一半模型可再 tool_search。**必须排在平台表之前**——「事件统计」
# 含「事件」会被 _QUERY_TOOLS 判成事件工单、「日常变更」含「变更」会命中变更单、
# 「演练合规」含「演练」会命中演练计划，顺序反了就永远到不了这里。
#
# **刻意避开跨平台/跨业务的裸词**：裸「视频」（视频监控是设备/文档域）、裸「演练」
# （= 演练计划查询）、裸「变更统计」（= mgmt.query_change_statistics）、裸「事件」
# （= 事件工单）。「提醒我」也带定时线索才认，避免「提醒我查一下昨天告警」被拐走。
# 桶之间按**首次命中**取，顺序＝特异性从高到低（change_notice 在 incident 之前，
# 否则「变更通报」里的「通报」会顺带 promote 事件统计工具）。
_BUILTIN_QUERY_TOOLS: list[tuple[tuple[str, ...], list[str]]] = [
  (('讲解视频', '生成视频', '做成视频', '视频讲解', '配音', r'重新合成', r'改.{0,6}解说'),
   ['video_create', 'video_create_from_text', 'video_status', 'video_pages',
    'video_update_page', 'video_update_pages', 'video_set_options', 'video_rerender']),
  (('飞书群', '飞书消息', '飞书私聊', '飞书群成员'),
   ['fetch_group_messages', 'fetch_p2p_messages', 'list_joined_chats', 'list_group_members']),
  (('日常变更', '变更通知', '变更通报', '变更日报'), ['change_notice_report']),
  (('关联告警', r'事件.{0,8}告警', r'告警.{0,8}关联'), ['event_alarm_link']),
  (('合规审查', r'演练.{0,6}(合规|审查|检查)'), ['drill_compliance_audit']),
  (('月度文档', r'文档.{0,4}(更新|检查|核对)'), ['doc_update_check']),
  (('事件统计', '速报', '通报', r'班次.{0,6}统计', '群消息'),
   ['incident_report', 'incident_progress', 'group_message_search']),
  (('定时任务', r'每天.{0,4}点', r'每周.{0,4}点', r'每月.{0,4}号',
    r'提醒我.{0,10}(每天|每周|每月|点|号|执行)'), ['schedule_task']),
]


def match_builtin_tools(message: str) -> list[str]:
  """扫内置扩展桶，返回首个命中条目的工具名（有序去重）。

  只返回工具名（不是 capability id）。命中多个桶时按 `_BUILTIN_QUERY_TOOLS` 的顺序
  取第一个，保证确定性、避免跨桶串味；未命中返回空。
  """
  if not message:
    return []
  for patterns, names in _BUILTIN_QUERY_TOOLS:
    if any(re.search(p, message, re.IGNORECASE) for p in patterns):
      return list(dict.fromkeys(names))
  return []


async def build_initial_state(
  message: str,
  catalog_hash: str,
  history: dict | None = None,
  *,
  task_mode: str = 'normal',
  web_search: bool = False,
  gated_names: tuple[str, ...] = (),
  known_names: frozenset[str] | None = None,
) -> dict:
  """跑完整 Step1+Step2，产出 agent 的 initial_state（含预 promote）。

  Args:
    message: 用户消息。
    catalog_hash: 当前 deferred catalog 指纹。
    history: 最近对话上下文 {'user_messages': [...], 'tools': [...]}，透传给 LLM plan
      供省略/延续追问（正则未命中的）判断指代；正则命中的完整查询不用历史。
    task_mode: 任务模式。联网调研模式（'research'）下额外把模式门控工具放出来。
    web_search: 前端「联网搜索」开关。与联网调研模式等价地放开门控工具，但**不注入**
      调研方法论（那是 MODE_INSTRUCTIONS 的事），刻意解耦。
    gated_names: 模式门控工具名（web__search / web__fetch 等）。它们不在
      tool_search 的检索目录里，**只能**靠这里预 promote 才可见 —— 这是「联网工具
      只在调研模式可用」的正向放开那一半（负向在 manifest/intent_bindings/extra_hidden）。
    known_names: 当前 deferred catalog 里的工具名集合（含内置扩展桶）。内置正则命中的
      工具按它过滤——开关关掉/未注册的工具不预 promote，状态干净。

  Returns:
    initial_state dict；无预 promote 时为空 dict（agent 正常走 tool_search）。
  """
  promoted = await _route(message, catalog_hash, history, known_names)
  names = list((promoted or {}).get('names') or [])
  gate_open = web_search or task_mode in MODE_GATED_TOOL_MODES
  if gate_open and gated_names:
    names = [*names, *[n for n in gated_names if n not in names]]
    logger.info(
      '放开联网工具（web_search=%s task_mode=%s）→ %d 个 %s',
      web_search, task_mode, len(gated_names), gated_names,
    )
  if not names:
    return {}
  return {'promoted': {'catalog_hash': catalog_hash, 'names': names}}


async def _route(
  message: str,
  catalog_hash: str,
  history: dict | None,
  known_names: frozenset[str] | None = None,
) -> dict | None:
  """意图路由（Step0 内置桶 → Step1 常用表 → Step2 正则 → Step3 LLM plan）。"""
  # 0) 内置扩展桶正则：命中即独占返回（这些是确定性流水线，不混平台工具），首轮免 tool_search
  builtin = match_builtin_tools(message)
  if known_names is not None:
    builtin = [n for n in builtin if n in known_names]
  if builtin:
    logger.info('route_to_tools(内置桶) → promote %d 工具 %s', len(builtin), builtin)
    return {'catalog_hash': catalog_hash, 'names': builtin}
  # 1) 常用查询精准表：命中即只 promote 该查询要用的几个工具（含解析类），首轮直接可用
  caps = match_query_caps(message)
  if caps:
    from service.query.base.tools_factory import capability_to_tool_name
    names = [capability_to_tool_name(c) for c in caps]
    logger.info('route_to_tools(常用表) → promote %d 工具 %s', len(names), caps)
    return {'catalog_hash': catalog_hash, 'names': names}
  # 2) 兜底：类别正则恰好命中 1 个 → 整组 promote（非工单类仍走这里）
  matched = regex_route(message)
  if len(matched) == 1:
    return route_to_tools(IntentPlan(intent_labels=matched), catalog_hash)
  # 未命中 / 命中多个（复合/模糊/省略延续/多主题）→ LLM plan 兜底，带最近上下文
  plan = await plan_query_safe(message, history)
  return route_to_tools(plan, catalog_hash)


async def plan_query_safe(message: str, history: dict | None = None) -> IntentPlan:
  """plan_query 的薄封装（plan_query 自身已兜底，这里保留扩展点）。"""
  from core.agent.query_planner import plan_query
  return await plan_query(message, history)
