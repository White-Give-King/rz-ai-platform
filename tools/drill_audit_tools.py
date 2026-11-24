"""应急演练记录合规审查工具。

口径全在 `service/drill_audit/`：定位演练周 → 下载 4 份班组 PDF（扫描件）→ OCR →
检索知识库 EOP → **每个班组独立判定** → 周级结论取最差 → 结构化结论。
原始 OCR 文本不进上下文，只回结论与证据。
"""

import json
import logging
from datetime import datetime

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig

from config.settings import settings
from core.agent.untrusted_content import sanitizing

logger = logging.getLogger(__name__)


def _err(message: str) -> str:
  return json.dumps({'error': message}, ensure_ascii=False)


def _get_thread_id(config: RunnableConfig | None) -> str:
  """从 RunnableConfig 取会话 thread_id（附件落在本会话目录，按会话归属鉴权）。"""
  if not config:
    return 'default'
  return (config.get('configurable') or {}).get('thread_id') or 'default'


@tool
@sanitizing
async def drill_compliance_audit(
  plan_name: str = '',
  week_id: str = '',
  dept_id: str = '',
  year: str = '',
  week: int = 0,
  groups: str = '',
  refresh: bool = False,
  config: RunnableConfig = None,
) -> str:
  """审查一次应急演练记录与知识库 EOP（应急操作流程）的差异，给出各组综合评价。

  平台会定位该演练计划对应的一周，下载该周 4 个班组（A/B/C/D）的演练记录 PDF
  （扫描件），OCR 后**每个班组各自独立**与知识库中对应的 EOP 比对，返回问题清单、
  按严重程度的统计与各组综合评价，并把明细做成可在对话里查看的附件。
  原始扫描件内容不返回，**不要**自己再下载/解析这些 PDF。
  同一周的记录平台只解析一次，之后复用（`note` 里出现「本次复用已解析记录 N 份」
  属正常，不是故障）。

  返回（JSON）：
  - 周级摘要（`summary`）：服务端确定性拼接的全周问题统计 + 各组一句话，
    可直接作为结论段引用。
  - 各班组评价（`groups`）：每班组一项，含班组字母、状态（ok / missing /
    ocr_failed / no_eop / judge_failed）、问题数、按严重程度的问题统计、该组综合评价。
    缺文件、OCR 失败、知识库无相关 EOP 或判定失败的班组也会出现，评价为空并带原因。
    有演练过程流程图时该组还带 `flow`（Mermaid 泳道流程图，含 ```mermaid 围栏），
    没有图的组不存在这个键。另带按维度的判定：`dimensions`（岗位分配 / 步骤完整性 /
    时限执行 / 操作合规 / 关键项 各一句结论 `达标|偏差|缺失|无法判定` + EOP 依据）、
    `missing_steps`（EOP 有、演练漏做的步骤）、`step_findings`（演练里做错的步：超时/违规，
    带步骤序号）、`timeline_stats`（步骤数与耗时汇总）。
  - 问题清单（`issues`）：扁平拼接，每条自带所属班组、问题、EOP 依据与演练记录证据；
    漏做步骤另带 `dimension="step_completeness"`，时间轴中做错的步另带 `seq`。
  - 附件（`attachments`）：问题明细交互表（`issues`）、演练过程记录（`records`，
    各组演练过程的时间轴文字与完整记录）。文字表在 `table_markdown`，文件卡片标签在
    `file_tag`。
  - 引用依据（`eop_refs`）：检索命中的 EOP 文档名与相似度，仅供需要时口头说明，
    不要做成表格。
  - 数据边界（`note`）：某班组记录缺失、OCR 失败、知识库无相关 EOP 或检索不可用，
    必须如实转述。

  **结论口径**：不判「合规 / 不合规」这类结论，改为**对每个班组给出综合评价**并按严重
  程度汇总问题数量；个别小问题不等于整周演练不合格。**按维度判、重点查漏**：讲评时先说
  该组哪些维度达标、哪些有偏差/缺失，**尤其要点出 `missing_steps`（EOP 要求有、演练实际
  没做的步骤）**——这是核心，不是抠文字字眼；并说明 `step_findings` 里超时/违规的步骤。
  **回答用语**：一律用中文业务表述（周级统计 / 各组综合评价 / 问题数 / 严重程度 /
  EOP 依据 / 演练记录证据），不要出现接口字段名这类英文词。**按班组分别陈述**，
  不要把不同班组混成一团。
  **交付流程图**：每个 `groups[].flow` 在该班组评价之后**整段原样输出**（连 ```mermaid
  围栏一起，节点文案、`<br/>`、`~~~` 一个字符都不能改，不得简写、不得自己重绘、不得把
  几组合并成一张）。没有 `flow` 的组不画图、不要猜。
  **交付附件**：`attachments` 里每个 `file_tag` 原样输出在回答末尾；有 `table_markdown`
  时原样放在对应小标题下（由服务端生成，不要自行增删列或改写列名）。演练过程的时间轴
  文字不要粘贴到回答里（由附件在线查看），流程图要贴。

  Args:
    plan_name: 演练计划名称（子串即可，如「制冷单元故障」）。与 `week_id` 二选一；
      匹配到多项时会返回候选让用户指明，此时不下载任何文件。
    week_id: 直接指定某次演练周 id（用户从页面拿到周 id 时才用）。
    dept_id: 部门 id；留空按当前用户的默认部门。说「A18 分部」这类简称时不要猜 id。
    year: 计划年份，留空取当年。
    week: 该计划第几周（数字）；留空取最近一个有记录的已安排周。
    groups: 只审查部分班组，如「A,B」；留空审查全部四个。
    refresh: 重新下载并解析班组记录（默认复用平台已解析过的同份记录）。
      仅当怀疑上游记录已被替换/更新时置 True；用户没提到「重新」就不要开。
  """
  if not settings.DRILL_AUDIT_ENABLED:
    return _err('应急演练合规审查未开启（DRILL_AUDIT_ENABLED=false）。')
  if not (plan_name or '').strip() and not (week_id or '').strip():
    return _err('请提供演练计划名称（plan_name）或演练周 id（week_id）之一。')

  from core.agent.user_context import get_current_user_id
  from service.drill_audit import pipeline

  uid = get_current_user_id()
  dept = (dept_id or '').strip()
  if not dept:
    try:
      from service.query.mgmt import http as mthttp
      _, dept = await mthttp.get_user_token_and_dept(uid)
    except Exception as e:  # noqa: BLE001 - 取不到默认部门时交给下游报错
      logger.info('[DrillAudit] 取默认部门失败: %s', str(e)[:120])

  async def _progress(message: str) -> None:
    try:
      from langchain_core.callbacks import adispatch_custom_event
      await adispatch_custom_event('drill_audit_status', {
        'tool': 'drill_compliance_audit', 'message': message,
      })
    except Exception:  # noqa: BLE001 - 进度只是反馈
      pass

  try:
    result = await pipeline.run_audit(
      plan_name=plan_name,
      week_id=week_id,
      dept_id=dept,
      year=year or datetime.now().year,
      week=int(week or 0),
      groups=groups,
      user_id=uid,
      progress=_progress,
      refresh=bool(refresh),
    )
  except ValueError as e:
    return _err(str(e))
  except Exception as e:  # noqa: BLE001
    logger.exception('[DrillAudit] 审查失败')
    return _err(f'审查失败：{type(e).__name__}: {str(e)[:200]}')

  try:
    from service.drill_audit import deliver
    attachments = await deliver.build_attachments(result, _get_thread_id(config))
    if attachments:
      result['attachments'] = attachments
  except Exception as e:  # noqa: BLE001 - 附件失败不影响审查结论
    logger.warning('[DrillAudit] 附件生成失败: %s', str(e)[:200])

  return json.dumps(result, ensure_ascii=False)
