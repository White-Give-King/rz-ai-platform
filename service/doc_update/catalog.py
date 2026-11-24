"""「平台每月所需更新文件目录（修订版）」的 36 条清单。

来源：PDF 单页 36 条（25 月度 / 4 实时 / 3 年度 / 1 年底 / 1 周 / 1 季度 / 1 半年度）。
本功能**只判定月度项**（`MONTHLY_ITEMS`，25 条），其余保留在清单里只为完整对照
（页面/工具可以说明「不在本功能范围」），不参与取数与判定。

字段说明：
- `numbers`：文档名开头的编号前缀。PDF 里 `1.6.6.1-1.6.6.3` 是区间，展开成三条；
  `1.7.1.6全量图纸、1.7.1.7全量设备资料` 是并列两条。
- `keywords`：同名不同表的消歧词（`4.1.5` 有四种隐患排查表）。**任一命中即可**，
  留空表示不按名称再过滤。
- `folders`：真实共享树里见到的目录/文件名（带前导编号，按实测登记）。PDF 里的清单名只是
  模糊叫法，**实际名称优先匹配**，`numbers` 作兜底。发现新写法只改这张表，不动匹配逻辑。
- `note`：PDF 的第三列（线上更新 / PDF / PPT / 文档），**决定判定口径**（见 `judge`）。
- `period_only`：只按期判定、**不下载不哈希**；期号允许从文件名解析（能源月报各部门
  把期号写在文件名里，目录只到年份；`4.1.5` 四条同理，目录只到年份、月份写在文件名里）。
- `exclude_paths`：**已作废的路径**（连续目录段），这些路径下的文件一律不匹配、不建索引根。
  用户明确废弃某个旧目录时登记在这里，不要靠删 `folders` 别名（别名在别处仍有效）。
"""

import re
from dataclasses import dataclass

CADENCE_MONTHLY = '月度'
IN_SCOPE_CADENCES: tuple[str, ...] = (CADENCE_MONTHLY,)

# 判定口径（一类文档一个口径，与所在部门的目录形态无关）：
# `线上更新` → 查更新（下载算 sha256，与上一期比内容是否变化）；
# 其余（PDF / PPT / 文档）→ 查新增（本月有没有新文件 / 本期期号是否已交）。
JUDGE_UPDATE = 'update'
JUDGE_NEW = 'new'


@dataclass(frozen=True)
class DocItem:
  seq: int
  numbers: tuple[str, ...]
  name: str
  cadence: str
  note: str = ''
  keywords: tuple[str, ...] = ()
  folders: tuple[str, ...] = ()
  period_only: bool = False
  exclude_paths: tuple[tuple[str, ...], ...] = ()

  @property
  def in_scope(self) -> bool:
    return self.cadence in IN_SCOPE_CADENCES

  @property
  def judge(self) -> str:
    """**基准口径**由 PDF 第三列决定：线上更新=查更新，其余=查新增。

    线上更新项若某部门实际是「按月新交」（路径有月份目录，或文件名能解析出
    多个月份），抓取阶段会按该部门的实际形态自动改判为查新增（见
    `period.auto_flip`），快照里存的是改判后的口径。
    """
    return JUDGE_UPDATE if self.note.strip() == '线上更新' else JUDGE_NEW

  @property
  def number_label(self) -> str:
    return '-'.join(self.numbers)


def _path_key(text) -> str:
  """目录名归一：去所有空白（含全角）+ 转小写。"""
  return ''.join(str(text or '').replace('\u3000', ' ').split()).lower()


def excluded_segments(item: DocItem, segments) -> bool:
  """`segments` 里是否出现该清单项登记的作废路径（连续段、忽略空白与大小写）。"""
  paths = item.exclude_paths or ()
  if not paths or not segments:
    return False
  names = [_path_key(s) for s in segments]
  for path in paths:
    key = [_path_key(p) for p in path]
    if not key or len(key) > len(names):
      continue
    for i in range(len(names) - len(key) + 1):
      if names[i:i + len(key)] == key:
        return True
  return False


CATALOG: tuple[DocItem, ...] = (
  DocItem(1, ('1.2.2',), '负载管理统计分析表', '月度', '线上更新'),
  # 实测同一份月报在 A1 树里有三处写法，1.4.5 那份最全（只登记编号会漏掉另两处）；
  # 后三条是通用写法，覆盖各部门「能源月报/能源报告」主题命名差异
  # 各部门形态不同：只有少数用 `2026年/10月` 目录，多数目录只到年份、期号在文件名里
  # （`…-2026-10.pdf`、`【20261031】`、`10、2026年…10月…`），故只看本期新增、不下载
  # 作废路径：`二线文件/2.客户报表/供配电/能源月报` 已停更（最新只到 2026-05），
  # 现在在用且最新的是 `1.4.5能源管理报告（能源月报同步）`；所有部门都排除该路径
  DocItem(2, ('1.4.5',), '能源月报', '月度', 'PPT', ('能源',), (
    '1.4.5能源管理报告', '1.2.3容量管理报告', '8.1.2.3能源管理月报',
    '能源管理报告', '能源月报', '能源报告',
  ), period_only=True, exclude_paths=(
    ('二线文件', '2.客户报表', '供配电', '能源月报'),
  )),
  DocItem(3, ('1.6.3.2',), '高低压配电柜继保整定表', '月度', '线上更新'),
  DocItem(4, ('1.6.3.3',), '动环报警阈值管理表', '月度', '线上更新'),
  DocItem(5, ('1.6.4',), '验证测试管理', '实时', '新项目相关测试文件需上传'),
  DocItem(6, ('1.6.6.1', '1.6.6.2', '1.6.6.3'), '供配电程序文档目录', '月度', '线上更新'),
  DocItem(7, ('1.7.1.6', '1.7.1.7'), '全量图纸与全量设备资料', '实时', '线上更新'),
  DocItem(8, ('2.2.3',), '设备配置状态表', '实时', '线上更新'),
  DocItem(9, ('2.2.4',), '设备操作记录表', '年度', 'PDF'),
  DocItem(10, ('2.2.5',), '灭火器检查卡', '年度', 'PDF'),
  DocItem(11, ('2.2.6',), '电池更换统计表', '实时', '线上更新'),
  DocItem(12, ('2.2.8',), '视频监控录像查询记录单', '月度', 'PDF'),
  DocItem(13, ('2.2.9',), '视频监控录像拷贝记录单', '月度', 'PDF'),
  DocItem(14, ('2.2.10',), '应急PDU月检记录表', '年底', 'PDF'),
  DocItem(15, ('2.3.2.2',), '应急卡使用登记表（中控室）', '月度', 'PDF'),
  DocItem(16, ('2.3.2.3',), '钥匙使用登记表（中控室）', '月度', 'PDF'),
  DocItem(17, ('2.3.2.4',), '窗户封条编号及更换登记表', '月度', 'PDF'),
  DocItem(18, ('2.3.2.5',), '打火机使用登记表（中控室）', '月度', 'PDF'),
  # 实测：三家（廊坊 A1/A8/A10）的月度记录放在无编号目录里，只按编号会只命中模板 xlsx
  DocItem(19, ('2.3.2.6',), '客户IT库房应急卡领取记录表', '月度', 'PDF', (), (
    '客户IT库房应急卡',   # 领取记录表 / 领取记录 / 领取登记表（A1、A8、A10）
    '客户库房应急卡',     # A7 `2.3.2.6…客户库房应急卡`、A6 `ZJ客户库房应急卡领取记录`
  )),
  DocItem(20, ('2.3.3.2',), '交接班签字表（中控室）', '月度', 'PDF'),
  DocItem(21, ('2.3.4.4',), '门禁巡检报警记录表', '月度', 'PDF'),
  DocItem(22, ('2.3.4.6',), '值班巡检记录表（部长 工程师 班长）', '月度', 'PDF'),
  DocItem(23, ('2.8.4',), '物品使用登记表（中控室）', '月度', 'PDF'),
  # 实测：廊坊 A1 的月度记录在无编号目录 `卫生清洁记录2026/<月>`（另有平铺旧目录
  # `卫生清洁记录`），只按编号 2.10.5 会命中章节目录里的模板 xlsx
  DocItem(24, ('2.10.5',), '卫生清洁记录表（保洁、技术员）', '月度', 'PDF', (), (
    '卫生清洁记录',
  )),
  DocItem(25, ('3.8.5',), '应急卡使用核查记录表', '周', '文档'),
  DocItem(26, ('3.8.6',), '门禁权限核查记录表', '季度', '文档'),
  # A1 的真实记录在 `3.8门禁权限管理/2026年/货物运输通道使用记录单/<月>`，
  # 编号 3.8.7 只命中钉点根上的模板 docx
  DocItem(27, ('3.8.7',), '货物运输通道使用记录单', '月度', 'PDF', (), (
    '货物运输通道使用记录单',
  )),
  DocItem(28, ('4.1.6.5',), '电伴热安全检查表', '月度', 'PDF'),
  DocItem(29, ('4.1.5',), '供配电隐患排查表', '月度', 'PDF', ('供配电',),
          period_only=True),
  DocItem(30, ('4.1.5',), '制冷隐患排查表', '月度', 'PDF', ('制冷',),
          period_only=True),
  DocItem(31, ('4.1.5',), '智能化隐患排查表', '月度', 'PDF', ('智能化',),
          period_only=True),
  DocItem(32, ('4.1.5',), '班组级隐患排查记录表', '月度', 'PDF', ('班组',),
          period_only=True),
  DocItem(33, ('4.1.6',), '梯子季度专项安全检查表', '年度', 'PDF', ('梯子',)),
  DocItem(34, ('6.3.1',), '人员排班计划表', '月度', '线上更新'),
  DocItem(35, ('6.3.2',), '考勤统计表', '月度', '线上更新'),
  DocItem(36, ('7.6.4',), '供应商考核信息统计表', '半年度', '线上更新'),
)

MONTHLY_ITEMS: tuple[DocItem, ...] = tuple(i for i in CATALOG if i.in_scope)


def summary() -> dict:
  """清单概览（页面/工具说明用，避免两处各写一份数字）。"""
  counts: dict[str, int] = {}
  for item in CATALOG:
    counts[item.cadence] = counts.get(item.cadence, 0) + 1
  return {
    'total': len(CATALOG),
    'in_scope': len(MONTHLY_ITEMS),
    'cadence_counts': counts,
  }


def _norm_text(text: str) -> str:
  """去所有空白（含全角空格）+ 转小写，供名称匹配。"""
  return ''.join((text or '').split()).lower()


_LEADING_NUMBER = re.compile(r'^\s*\d+(?:\.\d+)*')


def _alias_names(item: DocItem) -> tuple[str, ...]:
  """真实目录名去掉前导编号后的业务名（`1.4.5能源管理报告` → `能源管理报告`）。"""
  out = []
  for folder in item.folders:
    name = _norm_text(_LEADING_NUMBER.sub('', folder, count=1))
    if name:
      out.append(name)
  return tuple(out)


def resolve_items(query: str) -> list[DocItem]:
  """把用户说的「文件名」解析到清单项（只查 25 条月度项）。

  匹配有优先级，命中即返回：① 编号（`1.6.6.2` / `4.1.5制冷` 这类以编号开头的）
  ② 清单项名互相包含（`负载管理统计分析表`、真实上传文件名里也含它），
  **实际目录名也算**（`能源管理报告` → 能源月报）
  ③ 消歧关键词（`4.1.5` 四条靠 供配电/制冷/智能化/班组）。

  多义（「隐患排查表」「中控室」）返回多项，由调用方让用户指明；未命中返回 `[]`。
  """
  q = _norm_text(query)
  if not q:
    return []

  hits = [
    item for item in MONTHLY_ITEMS
    if any(q == n or q.startswith(f'{n}.') for n in item.numbers)
  ]
  if hits:
    return hits

  hits = [
    item for item in MONTHLY_ITEMS
    if q in _norm_text(item.name) or _norm_text(item.name) in q
    or any(q in a or a in q for a in _alias_names(item))
  ]
  if hits:
    return hits

  return [item for item in MONTHLY_ITEMS
          if any(_norm_text(k) in q for k in item.keywords)]
