"""演练计划 / 周明细 / 班组 PDF 下载（综合管理平台 mgmt）。"""

import logging

from service.query.mgmt import http as mthttp

logger = logging.getLogger(__name__)


async def list_plans(dept_id: str, year: int, user_id: str | None = None) -> list[dict]:
  """某部门某年的演练计划列表（接口返回纯数组、不分页）。"""
  data = await mthttp.request(
    'GET', '/drill/plan/list/v1',
    params={'deptId': dept_id, 'year': year}, user_id=user_id,
  )
  return data if isinstance(data, list) else []


async def week_detail(week_id: str, user_id: str | None = None) -> dict:
  """演练周明细：含四个班组的 `{a..d}groupFileId/groupFileName`。"""
  return await mthttp.request('GET', f'/drill/plan/week/v1/{week_id}', user_id=user_id)


async def download_pdf(
  file_id: str, *, dept_id: str | None = None, user_id: str | None = None,
) -> bytes:
  """下载班组演练记录 PDF 原始字节（唯一可用取数路径，见设计文档）。"""
  return await mthttp.download_bytes(
    f'/drill/plan/download/v1/{file_id}', dept_id=dept_id, user_id=user_id,
  )
