"""每日变更统计 → xlsx（变更统计明细 + 统计概览）。

样式对齐平台报告色板（与 `compliance/xlsx.py` 同一套）：表头浅蓝底 + 深蓝字、
横向细线、首行冻结、列宽按内容。0 行也出完整表头。

`page=True` 为页面导出模式：明细多一列「等级匹配」，变更类型 / 执行状态 /
等级匹配三列按值填底色（与页面胶囊同一套色）；默认（定时预设 / 对话 /
CLI）仍是 14 列、无底色。
"""

import io
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from service.scheduled_task.change_notice.build import (
  COLUMNS, MATCH_COLUMN, PAGE_COLUMNS, STATUS_ORDER, TYPE_ORDER,
)

DETAIL_SHEET = '变更统计明细'
OVERVIEW_SHEET = '统计概览'

# 重点列底色（与 ChangeNoticePage 的胶囊/色条同色系，浅底深字保证可读）
_TONE_FILLS = {
  '变更类型': {
    '临时新增紧急变更': 'FBE3E4',
    '高风险变更': 'FDF0E2',
    '中风险变更': 'E7F0F9',
    '低风险变更': 'E8F1FB',
    '未标注': 'F1F3F5',
  },
  '执行状态': {'已完成': 'E6F4EC', '进行中': 'E8F1FB', '已取消': 'F1F3F5'},
  MATCH_COLUMN: {'符合': 'E6F4EC', '不符合': 'FBE3E4', '无法判定': 'F1F3F5'},
}
_FILL_CACHE: dict[str, PatternFill] = {}


def _fill(code: str) -> PatternFill:
  fill = _FILL_CACHE.get(code)
  if fill is None:
    fill = _FILL_CACHE[code] = PatternFill('solid', fgColor=code)
  return fill


_HEAD_FILL = PatternFill('solid', fgColor='EAF1FA')
_HEAD_FONT = Font(color='134B70', bold=True, size=10)
_TITLE_FONT = Font(color='134B70', bold=True, size=12)
_BODY_FONT = Font(size=10)
_THIN = Side(style='thin', color='DBE7F3')
_BORDER = Border(bottom=_THIN)
_WRAP_TOP = Alignment(vertical='top', wrap_text=True)
_CENTER = Alignment(horizontal='center', vertical='center')

_WIDTHS = {
  '园区': 8, '数据中心': 10, '变更类型': 16, '维护事项': 40, '执行状态': 10,
  '计划开始': 18, '计划结束': 18, '实际开始': 18, '实际结束': 18,
  '工单号': 30, '变更等级': 10, '现场负责人': 18, '现场负责人等级': 12,
  MATCH_COLUMN: 12, '消息时间': 20, '合计': 8,
}
_CENTER_COLUMNS = {'园区', '数据中心', '变更类型', '执行状态', '变更等级',
                   '现场负责人等级', MATCH_COLUMN, '合计'}
_CENTER_COLUMNS.update(TYPE_ORDER)
_CENTER_COLUMNS.update(STATUS_ORDER)


def _width_for(name: str) -> float:
  return _WIDTHS.get(name, min(30, max(12, len(name) * 2 + 4)))


def _apply_width(ws, index: int, name: str) -> None:
  letter = get_column_letter(index)
  current = ws.column_dimensions[letter].width
  width = _width_for(name)
  if current is None or current < width:
    ws.column_dimensions[letter].width = width


def _write_title(ws, text: str, row: int = 1) -> int:
  if not text:
    return row
  ws.cell(row=row, column=1, value=text).font = _TITLE_FONT
  return row + 1


def _write_header(ws, columns, row: int) -> None:
  for i, name in enumerate(columns, 1):
    cell = ws.cell(row=row, column=i, value=name)
    cell.fill = _HEAD_FILL
    cell.font = _HEAD_FONT
    cell.alignment = _CENTER
    cell.border = _BORDER
    _apply_width(ws, i, name)


def _write_rows(ws, columns, rows: list[dict], start_row: int, tones: bool = False) -> int:
  row = start_row
  for data in rows:
    for i, name in enumerate(columns, 1):
      value = data.get(name, '')
      cell = ws.cell(row=row, column=i, value='' if value is None else value)
      cell.font = _BODY_FONT
      cell.border = _BORDER
      cell.alignment = _CENTER if name in _CENTER_COLUMNS else _WRAP_TOP
      if tones:
        code = _TONE_FILLS.get(name, {}).get(str(cell.value))
        if code:
          cell.fill = _fill(code)
    row += 1
  return row


def _overview_rows(groups: list[dict]) -> list[dict]:
  rows = []
  for group in groups:
    row = {'园区': group.get('park', ''), '合计': group.get('total', 0)}
    row.update({k: v for k, v in (group.get('by_type') or {}).items()})
    row.update({k: v for k, v in (group.get('by_status') or {}).items()})
    rows.append(row)
  return rows


def _build_workbook(rows: list[dict], groups: list[dict],
                    title: str = '', period: str = '', *, page: bool = False) -> Workbook:
  """明细 + 概览两张 sheet。`page=True`：多一列等级匹配、重点列填底色。"""
  wb = Workbook()
  columns = PAGE_COLUMNS if page else COLUMNS
  detail_rows = rows
  if page:
    detail_rows = [{**r, MATCH_COLUMN: r.get(MATCH_COLUMN, r.get('__match', ''))}
                   for r in rows]

  detail = wb.active
  detail.title = DETAIL_SHEET
  header_row = _write_title(detail, f'{title or DETAIL_SHEET} · {period}' if period else title)
  _write_header(detail, columns, header_row)
  _write_rows(detail, columns, detail_rows, header_row + 1, tones=page)
  detail.freeze_panes = f'A{header_row + 1}'

  overview = wb.create_sheet(OVERVIEW_SHEET)
  overview_columns = ('园区', *TYPE_ORDER, *STATUS_ORDER, '合计')
  head = _write_title(overview, f'统计概览 · {period}' if period else '统计概览')
  _write_header(overview, overview_columns, head)
  _write_rows(overview, overview_columns, _overview_rows(groups), head + 1)
  overview.freeze_panes = f'A{head + 1}'
  return wb


def write_workbook(path: str | Path, rows: list[dict], groups: list[dict],
                   title: str = '', period: str = '') -> Path:
  """定时预设 / 对话 / CLI 用：14 列明细 + 概览，返回落盘路径（行为不变）。"""
  wb = _build_workbook(rows, groups, title, period)
  target = Path(path)
  target.parent.mkdir(parents=True, exist_ok=True)
  wb.save(target)
  return target


def to_bytes(rows: list[dict], groups: list[dict],
             title: str = '', period: str = '', *, page: bool = False) -> bytes:
  """页面导出用：不落盘，直接返回 xlsx 字节流。"""
  buf = io.BytesIO()
  _build_workbook(rows, groups, title, period, page=page).save(buf)
  return buf.getvalue()
