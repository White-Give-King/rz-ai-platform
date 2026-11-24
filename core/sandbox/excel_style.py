"""沙箱内 Excel 样式助手**唯一来源** —— 让模型生成的 xlsx 也能「按颜色词上色」。

背景：确定性导出的 xlsx（`change_notice/xlsx.py` 的 `_TONE_FILLS`）有配色能力，但
**模型在沙箱里现写代码生成 Excel 时只有 `df.to_excel()`，没有任何样式手段** —— 用户拿到
「标识颜色 = 绿色/橙色/红色」这种列时，看到的只是文字，没法一眼看出颜色（实测反馈）。

做法照 `report_style.py`：把源码作为字符串（`RZ_EXCEL_SRC`）导出，由
`core/sandbox/runner.py` 注入子进程的 `_WRAPPER_TEMPLATE`（`__RZ_EXCEL__` 哨兵）。
原因同上：沙箱是 `python -I` 隔离执行，忽略 PYTHONPATH，**不能放独立 py 文件让沙箱
import**，只能把源码文本塞进 wrapper。注入的只是**定义**，`openpyxl` 在函数内部惰性
import —— 不产 Excel 的脚本零开销。

颜色词映射与 `service/scheduled_task/change_notice/xlsx.py` 的 `_TONE_FILLS` 同色系
（柔和底色，不刺眼），并补全常见颜色词。
"""

RZ_EXCEL_SRC: str = r'''
# ---- 平台 Excel 样式助手（由 core/sandbox/excel_style.py 注入，仅供沙箱内使用）----
# 颜色词 -> 柔和底色（十六进制，无 #）。与平台确定性导出的 `_TONE_FILLS` 同色系。
RZ_XLSX_FILLS = {
    '红': 'FBE3E4', '橙': 'FDF0E2', '黄': 'FFF6DA', '绿': 'E6F4EC',
    '蓝': 'E8F1FB', '紫': 'F0E9F8', '灰': 'F1F3F5', '青': 'E0F3F2',
    '黑': 'D9D9D9', '白': 'F7F7F7',
}
RZ_XLSX_HEAD_FILL = 'EAF1FA'   # 表头浅蓝底
RZ_XLSX_HEAD_FONT = '1F3A5F'   # 表头深蓝字


def _rz_xlsx_cell_value(v):
    """把 pandas/numpy 取值归一成 openpyxl 能写的原生类型；缺失值 -> None。"""
    if v is None:
        return None
    # numpy 标量 -> Python 原生
    if hasattr(v, 'item') and not isinstance(v, (str, bytes, bytearray)):
        try:
            v = v.item()
        except Exception:
            pass
    # NaN / NaT
    try:
        if isinstance(v, float) and v != v:
            return None
    except Exception:
        pass
    if type(v).__name__ == 'NaTType':
        return None
    return v


def _rz_xlsx_fill_of(value):
    """单元格文本 -> 底色 hex；不是颜色词返回 None。认「绿色」也认裸字「绿」。"""
    if value is None or isinstance(value, (bytes, bytearray)):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith('色'):
        text = text[:-1]
    return RZ_XLSX_FILLS.get(text)


def rz_excel(df, path, color_columns=None, sheet='Sheet1'):
    """把 DataFrame 写成带样式的 xlsx：表头加粗浅蓝底、冻结首行，
    并对 `color_columns`（默认=全部列）里取值是颜色词（绿色/橙色/红色…）的单元格自动填色。

    用法：
        rz_excel(df, '变更工单统计.xlsx', color_columns=['标识颜色'])

    只负责「写一张带色表」，不改 df、不做业务判断。openpyxl 在此惰性 import。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    if color_columns is None:
        color_columns = list(df.columns)
    elif isinstance(color_columns, str):
        color_columns = [color_columns]
    color_idx = {df.columns.get_loc(c) for c in color_columns if c in df.columns}

    wb = Workbook()
    ws = wb.active
    ws.title = str(sheet)[:31] or 'Sheet1'

    head_font = Font(bold=True, color='FF' + RZ_XLSX_HEAD_FONT)
    head_fill = PatternFill('solid', fgColor=RZ_XLSX_HEAD_FILL)
    thin = Side(style='thin', color='D9DEE5')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left = Alignment(horizontal='left', vertical='center', wrap_text=True)

    columns = list(df.columns)
    for i, name in enumerate(columns, 1):
        cell = ws.cell(row=1, column=i, value=str(name))
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = center
        cell.border = border

    widths = [len(str(n)) for n in columns]
    for r, row in enumerate(df.itertuples(index=False, name=None), start=2):
        for i, raw in enumerate(row, 1):
            value = _rz_xlsx_cell_value(raw)
            cell = ws.cell(row=r, column=i, value=value)
            cell.alignment = left
            cell.border = border
            # 自动上色（列下标从 0 起；color_idx 用 df.columns 位置）
            if (i - 1) in color_idx:
                code = _rz_xlsx_fill_of(value)
                if code:
                    cell.fill = PatternFill('solid', fgColor=code)
            text = '' if value is None else str(value)
            if len(text) > widths[i - 1]:
                widths[i - 1] = len(text)

    ws.freeze_panes = 'A2'
    for i, w in enumerate(widths, 1):
        # 中文偏宽，按 1.6 估宽；夹在 8~40 之间
        ws.column_dimensions[get_column_letter(i)].width = min(40, max(8, w * 1.6))

    wb.save(str(path))
    return str(path)
'''
