"""공개 성과 스냅샷 굽기 검증 — 오프라인·무시크릿·실패 무해.

  1) ntfy 최신 alpha-dash → 앱 스키마(markets/days/generated_at)로 변환·생성
  2) ntfy 실패 시 캐시 폴백(오후 만료 문제 해소의 핵심)
  3) 소스 전무 → 파일 미생성·False (빈 스키마를 지어내지 않음)
  4) 미확정(None) 값이 0으로 강등되지 않고 보존

실행: python -m tests.test_perf_site
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner import perf_site as P


_PAYLOAD = {
    "day": {"US": {"date": "2026-08-11",
                   "series": [["22:30", 0.15, 0.20], ["22:37", None, -0.19]]}},
    "days": [
        {"d": "2026-08-10", "mkt": "US", "acct": 0.5, "idx": 0.38,
         "a": 0.9, "b": None, "quality": "ok", "indices": {"나스닥": 0.38}},
        {"d": "2026-08-11", "mkt": "US", "acct": None, "idx": 0.1,
         "a": None, "b": None, "quality": "partial", "indices": {"나스닥": 0.1}},
    ],
}


def _ntfy_lines(payload) -> bytes:
    rows = [
        {"event": "open"},
        {"event": "message", "title": "other", "time": 1, "message": "x"},
        {"event": "message", "title": "alpha-dash", "time": 1786500000,
         "message": json.dumps(payload)},
    ]
    return "\n".join(json.dumps(r) for r in rows).encode()


class _Resp:
    def __init__(self, body: bytes): self._b = body
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _sandbox():
    tmp = tempfile.mkdtemp()
    P.CACHE_PATH = os.path.join(tmp, "cache", "perf_snapshot.json")
    return tmp


def test_build_from_ntfy():
    tmp = _sandbox()
    with mock.patch.object(P.urllib.request, "urlopen",
                           lambda url, timeout=0: _Resp(_ntfy_lines(_PAYLOAD))):
        assert P.build(out_dir=os.path.join(tmp, "public"))
    out = json.load(open(os.path.join(tmp, "public", "api", "performance.json"),
                         encoding="utf-8"))
    assert out["markets"]["US"]["series"], out["markets"]["US"]
    assert out["generated_at"]                     # 발행 시각 주입
    assert out["read_only"] is True and out["source"] == "actions-ntfy"
    # None(미확정) 보존 — 0으로 강등 금지
    day2 = [r for r in out["days"] if r["date"] == "2026-08-11"][0]
    assert day2["account"] is None and day2["A"] is None
    assert os.path.exists(P.CACHE_PATH)            # 다음 실행용 캐시 보존
    print("[PASS] ntfy → 앱 스키마 변환·미확정 보존·캐시 저장")


def test_cache_fallback_when_ntfy_empty():
    tmp = _sandbox()
    os.makedirs(os.path.dirname(P.CACHE_PATH))
    json.dump({"payload": _PAYLOAD, "published_at": 1786500000},
              open(P.CACHE_PATH, "w", encoding="utf-8"))

    def fail(url, timeout=0):
        raise OSError("network down")

    with mock.patch.object(P.urllib.request, "urlopen", fail):
        assert P.build(out_dir=os.path.join(tmp, "public"))
    out = json.load(open(os.path.join(tmp, "public", "api", "performance.json"),
                         encoding="utf-8"))
    assert out["source"] == "actions-cache" and out["markets"]["US"]["series"]
    print("[PASS] ntfy 실패 → 캐시 폴백(만료 없는 사이트 사본)")


def test_no_source_writes_nothing():
    tmp = _sandbox()
    def fail(url, timeout=0):
        raise OSError("network down")
    with mock.patch.object(P.urllib.request, "urlopen", fail):
        assert not P.build(out_dir=os.path.join(tmp, "public"))
    assert not os.path.exists(os.path.join(tmp, "public", "api",
                                           "performance.json"))
    print("[PASS] 소스 전무 → 파일 미생성(빈 데이터 발명 금지)")


def test_no_secrets_in_output():
    tmp = _sandbox()
    with mock.patch.object(P.urllib.request, "urlopen",
                           lambda url, timeout=0: _Resp(_ntfy_lines(_PAYLOAD))), \
            mock.patch.dict(os.environ, {"KIS_MOCK_APPKEY": "SECRETKEY999"}):
        P.build(out_dir=os.path.join(tmp, "public"))
    raw = open(os.path.join(tmp, "public", "api", "performance.json"),
               encoding="utf-8").read()
    assert "SECRETKEY999" not in raw and "계좌" not in json.dumps(
        json.loads(raw).get("days"))               # 금액·계좌번호 필드 없음
    print("[PASS] 출력 무시크릿(퍼센트만)")


def main():
    test_build_from_ntfy()
    test_cache_fallback_when_ntfy_empty()
    test_no_source_writes_nothing()
    test_no_secrets_in_output()
    print("\n공개 성과 스냅샷 굽기 검증 통과 — 앱 성과 탭 공개 사이트 작동.")


if __name__ == "__main__":
    main()
