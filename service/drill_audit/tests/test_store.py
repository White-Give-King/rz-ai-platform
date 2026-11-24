"""内容库纯函数自检：不连 Mongo、不联网（临时目录 + 纯函数）。

运行：`python -m service.drill_audit.tests.test_store`
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import settings
from service.drill_audit import store

_SHA_A = 'ab' + '0' * 62
_SHA_B = 'cd' + '1' * 62


class _TempDir:
  """临时替换 `DRILL_AUDIT_STORE_DIR`，退出时还原。"""

  def __enter__(self) -> Path:
    self._saved = settings.DRILL_AUDIT_STORE_DIR
    self._tmp = tempfile.TemporaryDirectory()
    settings.DRILL_AUDIT_STORE_DIR = self._tmp.name
    return Path(self._tmp.name)

  def __exit__(self, *exc):
    settings.DRILL_AUDIT_STORE_DIR = self._saved
    self._tmp.cleanup()


def t_paths():
  pdf, txt = store.content_paths(_SHA_A)
  assert pdf.name == f'{_SHA_A}.pdf' and txt.name == f'{_SHA_A}.txt'
  assert pdf.parent.name == 'ab' and pdf.parent.parent.name == 'sha256'
  pdf_b, _ = store.content_paths(_SHA_B)
  assert pdf_b.parent.name == 'cd'
  assert store.sha256_of(b'abc')[0] == 'b' and len(store.sha256_of(b'abc')) == 64


def t_write_read():
  with _TempDir():
    assert store.read_text(_SHA_A) is None
    written = store.write_content(_SHA_A, pdf=b'%PDF-1.4', text='演练记录 A 班组')
    assert written['size_pdf'] == 8 and written['chars'] == 9
    assert Path(written['pdf_path']).exists() and Path(written['text_path']).exists()
    assert store.read_text(_SHA_A) == '演练记录 A 班组'
    # 空文本不落文件；已存在的 pdf 不因本次只写文本而被删除
    empty = store.write_content(_SHA_A, text='')
    assert empty['text_path'] == '' and empty['chars'] == 0
    assert Path(written['pdf_path']).exists()


def t_atomic_overwrite():
  with _TempDir():
    store.write_content(_SHA_A, text='旧')
    store.write_content(_SHA_A, text='新')
    assert store.read_text(_SHA_A) == '新'
    parent = store.content_paths(_SHA_A)[0].parent
    assert not list(parent.glob('*.tmp')), '不应残留临时文件'


def t_count_bytes():
  rows = [
    {'size_pdf': 100, 'size_text': 10},
    {'size_pdf': 0, 'size_text': 5},
  ]
  assert store.count_bytes(rows) == 115
  assert store.count_bytes([]) == 0


def _row(file_id, pdf, text, present_pdf=True, present_text=True, age_h=0):
  return {
    '_id': file_id,
    'size_pdf': pdf,
    'size_text': text,
    'pdf_present': present_pdf,
    'text_present': present_text,
    'last_used_at': datetime.now(timezone.utc) - timedelta(hours=age_h),
  }


def t_plan_evictions():
  # 未超上限：不动
  rows = [_row('f1', 100, 10), _row('f2', 100, 10, age_h=1)]
  assert store.plan_evictions(rows, 1024) == []
  assert store.plan_evictions(rows, 0) == []

  # 超上限：先删 PDF 保 OCR 成果，按 last_used_at 升序（f2 更久未用先删）
  plan = store.plan_evictions(rows, 150)
  assert plan == [('f2', 'pdf')], plan   # 220-100=120 ≤ 135

  # 删完 PDF 仍超：继续删 PDF（同一轮），直到 90%
  plan = store.plan_evictions(rows, 100)
  assert [k for _, k in plan] == ['pdf', 'pdf'], plan

  # 已无 PDF 可删：退而删 TXT
  text_only = [
    _row('f1', 0, 30, present_pdf=False, age_h=2),
    _row('f2', 0, 30, present_pdf=False, age_h=1),
  ]
  plan = store.plan_evictions(text_only, 40)
  assert plan == [('f1', 'text')], plan   # 60-30=30 ≤ 36

  # 存在标志为假的行不参与淘汰
  gone = [_row('f1', 100, 10, present_pdf=False, present_text=False)]
  assert store.plan_evictions(gone, 50) == []


def main():
  checks = [t_paths, t_write_read, t_atomic_overwrite, t_count_bytes, t_plan_evictions]
  for fn in checks:
    fn()
    print(f'ok {fn.__name__}')
  print(f'\n{len(checks)} 项自检全部通过')


if __name__ == '__main__':
  main()
