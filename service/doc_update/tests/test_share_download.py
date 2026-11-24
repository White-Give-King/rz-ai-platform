"""共享文件下载自检（不联网）：必须走 POST `/file/share/v1/download/{fid}`。

2026-11-12 事故：原实现调 `GET /file/share/v1/minio/url/{fid}` 取预签名地址，服务端对服务
账号恒 403，6 个「线上更新」项每次都被判下载失败（还连带触发 KeyError）。真实入口是
POST 直出文件流（两个服务账号实测 200）。

跑法：`python -m service.doc_update.tests.test_share_download`
"""

import asyncio

from config.settings import settings
from service.doc_update import fetch, share

_OLD_STREAM = fetch.stream_sha256


def _install(fake):
  fetch.stream_sha256 = fake


def _restore():
  fetch.stream_sha256 = _OLD_STREAM


def test_posts_to_download_endpoint():
  calls = []

  async def fake(url, method='GET'):
    calls.append((url, method))
    return 'deadbeef', 1234, b'PK'

  _install(fake)
  try:
    res = asyncio.run(share.download_sha256('fid123'))
  finally:
    _restore()
  expected = f'{settings.MGMT_BASE_URL.rstrip("/")}/file/share/v1/download/fid123'
  assert calls == [(expected, 'POST')], f'下载入口必须是 POST download，实际 {calls}'
  assert res['sha256'] == 'deadbeef' and res['download_size'] == 1234
  assert res['error'] == ''


def test_failure_returns_error_not_raise():
  async def boom(url, method='GET'):
    raise RuntimeError('boom')

  _install(boom)
  try:
    res = asyncio.run(share.download_sha256('fid123'))
  finally:
    _restore()
  assert res['sha256'] == '' and res['download_size'] == 0
  assert 'boom' in res['error'], '失败要落在 error 字段，不能抛出中断整轮'


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'全部通过：{len(tests)} 项')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
