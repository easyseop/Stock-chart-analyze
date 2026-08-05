"""서버 자가진단 발행 검증 — 읽기 전용·무시크릿·실패 무해.

실행: python -m tests.test_ops_status
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import ops_status  # noqa: E402


def _quiet_probes():
    """외부 의존(브로커·systemd·피드)을 결정론적으로 대체."""
    from bot import kis, sentinel
    return [
        mock.patch.object(kis, "positions_detail",
                          lambda market="US", excg="NASD": [] if market == "KR"
                          else ([{"code": "AAPL"}] if excg == "NASD" else [])),
        mock.patch.object(sentinel, "_fetch_positions",
                          lambda: ([], 12.0)),
        mock.patch.object(subprocess, "run",
                          side_effect=RuntimeError("no systemd")),
    ]


def test_snapshot_shape_and_no_secrets():
    env = {"KIS_ENV": "mock", "TRADE_STAGE": "mirror", "ALLOW_BUY": "1",
           "KIS_ORDERS_ENABLED": "1",
           "KIS_MOCK_APPKEY": "SECRETKEY123", "KIS_MOCK_APPSECRET": "SECRET456",
           "KIS_MOCK_CANO": "50009999", "TELEGRAM_BOT_TOKEN": "TGTOKEN789"}
    patches = _quiet_probes()
    with patches[0], patches[1], patches[2], \
            mock.patch.dict(os.environ, env):
        snap = ops_status.snapshot()
    assert snap["v"] == 1 and snap["generated_at"]
    assert snap["kis_positions_query"]["KR"] == 0          # 성공(0보유)
    assert snap["kis_positions_query"]["NASD"] == 1
    assert snap["kis_query_ok"] is True
    assert snap["positions_feed_age_min"] == 12.0
    assert snap["flags"]["kis_env"] == "mock"
    assert snap["flags"]["stage"] == "mirror"
    # 시크릿·계좌번호·토큰이 직렬화 어디에도 없어야 한다.
    text = json.dumps(snap, ensure_ascii=False)
    for secret in ("SECRETKEY123", "SECRET456", "50009999", "TGTOKEN789"):
        assert secret not in text, secret
    print("[PASS] 스냅샷 구조 + 시크릿·계좌번호 0")


def test_query_failure_is_reported_not_raised():
    from bot import kis, sentinel
    with mock.patch.object(kis, "positions_detail",
                           lambda market="US", excg="NASD": None), \
            mock.patch.object(sentinel, "_fetch_positions",
                              side_effect=RuntimeError("feed down")), \
            mock.patch.object(subprocess, "run",
                              side_effect=RuntimeError("no systemd")):
        snap = ops_status.snapshot()
    assert snap["kis_query_ok"] is False                   # 실패 = 값으로 보고
    assert all(v is None for v in snap["kis_positions_query"].values())
    assert snap["feed_error"] == "RuntimeError"
    print("[PASS] 조회 실패도 예외 없이 값으로 보고(fail-visible)")


def test_publish_posts_to_ops_topic_and_failure_is_harmless():
    sent = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ok = ops_status.publish({"v": 1, "kill_level": 1})
    assert ok and "ntfy.sh" in sent["url"]
    from bot import settings
    assert settings.OPS_STATUS_TOPIC in sent["url"]
    assert sent["body"]["kill_level"] == 1
    with mock.patch("urllib.request.urlopen",
                    side_effect=OSError("network down")):
        assert ops_status.publish({"v": 1}) is False       # 실패 무해
    print("[PASS] ntfy 발행 + 네트워크 실패 무해(False)")


def test_maybe_publish_respects_interval():
    calls = []
    with mock.patch.object(ops_status, "publish",
                           side_effect=lambda *a: calls.append(1) or True):
        ops_status._last_publish = 0.0
        assert ops_status.maybe_publish() is True          # 첫 호출 발행
        assert ops_status.maybe_publish() is False         # 간격 내 — 발행 없음
    assert len(calls) == 1
    print("[PASS] 주기 발행 간격 준수(폭주 방지)")


def test_read_only_no_order_paths():
    import inspect
    src = inspect.getsource(ops_status)
    for banned in ("place_buy", "place_sell", "execute_entry", "raise_level",
                   "lower_level"):
        assert banned not in src, banned
    print("[PASS] 주문·kill 변경 경로 0(읽기 전용)")


def main():
    test_snapshot_shape_and_no_secrets()
    test_query_failure_is_reported_not_raised()
    test_publish_posts_to_ops_topic_and_failure_is_harmless()
    test_maybe_publish_respects_interval()
    test_read_only_no_order_paths()
    print("\n서버 자가진단 발행 검증 통과 — 읽기전용·무시크릿·실패무해.")


if __name__ == "__main__":
    main()
