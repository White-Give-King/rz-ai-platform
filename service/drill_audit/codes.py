"""演练计划码表与检索词构造。

专业 id 跨部门一致（2026-11-16 在 A18 分部实测枚举，7 个固定值）。
"""

# 演练专业：id → 中文名（平台侧 drillSpecialityName 是英文，这里给中文便于阅读/检索）
DRILL_SPECIALITIES: dict[str, str] = {
  '285gLxiBnka': '智能化',
  '285gLxiBnkb': '制冷',
  '285gLxiBnkc': '供配电',
  '285gLxiBnkd': '公共安全',
  '285gLxiBnke': '叠加',
  '285gLxiBnkf': '生产安全事件',
  '285gLxiBnkg': '环境事件',
}

# 周完成状态（finishStatus，接口返回字符串）
FINISH_STATUS: dict[str, str] = {
  '1': '未开始', '2': '进行中', '3': '已完成', '4': '已逾期',
}

# 周安排状态（status）
WEEK_STATUS: dict[int, str] = {0: '已安排', 1: '未安排'}

_GROUP_LETTERS = ('A', 'B', 'C', 'D')


def speciality_name(speciality_id: str) -> str:
  """专业 id → 中文名；未知返回空串（调用方保留原始 id）。"""
  return DRILL_SPECIALITIES.get(str(speciality_id or ''), '')


def speciality_hint(name: str) -> str:
  """由计划名构造 EOP 检索词。

  计划名形如「补水系统故障+制冷单元故障应急操作演练」，去掉演练后缀再拼
  「应急操作流程」即可与知识库里 EOP 的命名对齐。
  """
  base = (name or '').strip()
  for suffix in ('应急操作演练', '应急演练', '演练'):
    if base.endswith(suffix):
      base = base[: -len(suffix)]
      break
  return f'{base} 应急操作流程'.strip()


def group_file(detail: dict, letter: str) -> tuple[str, str]:
  """从周明细取某班组的 `(file_id, file_name)`；缺失或空均返回 `('', '')`。"""
  key = letter.lower()
  return (
    str(detail.get(f'{key}groupFileId') or ''),
    str(detail.get(f'{key}groupFileName') or ''),
  )


def normalize_groups(groups: str | None) -> list[str]:
  """`'A,B'` / `'ab'` → `['A','B']`；空 → 全部四个班组。只认 A~D。"""
  raw = [c.strip().upper() for c in str(groups or '').replace('，', ',').split(',')]
  picked = [c for c in raw if c in _GROUP_LETTERS]
  seen: list[str] = []
  for c in picked:
    if c not in seen:
      seen.append(c)
  return seen or list(_GROUP_LETTERS)
