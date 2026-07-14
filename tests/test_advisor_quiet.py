"""제안·페이퍼 매도 알림 기본 OFF 검증 — 사용자 요청(2026-07-12, 2026-07-14).

  1) 기본(플래그 없음): 신선한 now 신호가 있어도 '매수 제안'·'도달' 텔레그램 0건
  2) ADVISOR_SUGGEST_ALERTS=1: 기존대로 매수 제안 발송(스위치 가역성)
  3) 페이퍼/피드 매도 제안 경보도 기본 OFF(ADVISOR_ALERTS=1로만 재활성) —
     실계좌 손절 알림은 KIS 파수꾼(sentinel)이 '🛡️ 파수꾼 매도'로 직접 발송.

실행: python -m tests.test_advisor_quiet
"""
from __future__ import annotations

import datetime
import importlib
import json
import os
import sys
import tempfile
import types
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_signals(path):
    now = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))).isoformat()
    json.dump({"generated_at": now,
               "signals": [{"group": "now", "id": "X1", "code": "XX",
                            "name": "테스트", "ccy": "USD", "entry": 100,
                            "stop": 95, "fresh": True, "stage": 3,
                            "norm": 50, "tactic": {}}]},
              open(path, "w"))


def _run(tmp, flag: str | None):
    os.environ.pop("ADVISOR_SUGGEST_ALERTS", None)
    if flag:
        os.environ["ADVISOR_SUGGEST_ALERTS"] = flag
    import bot.settings as cfg
    import bot.advisor as advisor
    importlib.reload(cfg)
    importlib.reload(advisor)
    sent = []
    advisor.notify.send = lambda t, **kw: sent.append(t) or True
    advisor.cfg.market_open = lambda ccy: True
    advisor._held_codes = lambda: set()
    advisor.STATE_PATH = os.path.join(tmp, "alert_state.json")
    sig = os.path.join(tmp, "signals.json")
    _fresh_signals(sig)
    args = types.SimpleNamespace(signals=sig,
                                 holdings=os.path.join(tmp, "h.json"),
                                 dry_run=False)
    advisor.run_once(args)
    return advisor, sent


def test_default_quiet():
    with tempfile.TemporaryDirectory() as tmp:
        _, sent = _run(tmp, flag=None)
        assert not [m for m in sent if "매수 제안" in m], "기본값인데 매수 제안 발송"
        assert not [m for m in sent if "도달" in m]
    print("[PASS] 기본: 신선한 신호여도 매수 제안·도달 알림 0건")


def test_flag_reenables():
    with tempfile.TemporaryDirectory() as tmp:
        _, sent = _run(tmp, flag="1")
        assert [m for m in sent if "매수 제안" in m], "플래그 켰는데 제안 미발송"
    print("[PASS] ADVISOR_SUGGEST_ALERTS=1 → 제안 발송(가역)")


def test_sell_alert_gated_off_by_default():
    """페이퍼/피드 매도 제안 경보는 기본 OFF(사용자 요청 2026-07-14).

    실계좌(KIS 모의) 손절은 파수꾼(sentinel)이 실시간가로 감시·집행하고
    자체 '🛡️ 파수꾼 매도' 알림을 보낸다 → 페이퍼 경보는 중복이라 끈다.
    감지 카운트(n)는 유지(대시보드·로깅용), 텔레그램 발송만 억제.
    """
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.pop("ADVISOR_ALERTS", None)
        advisor, sent = _run(tmp, flag=None)
        with mock.patch.object(advisor, "_quote", return_value=90.0):
            n = advisor.check_sell_alerts(
                [{"code": "XX", "name": "테스트", "ccy": "USD",
                  "qty": 3, "avg": 100.0, "stop": 95.0, "target": 120.0}],
                {}, dry_run=False)
        assert n == 1                                       # 감지는 계속
        assert not [m for m in sent if "손절" in m], "기본인데 페이퍼 손절 경보 발송"
    print("[PASS] 페이퍼 매도 제안 경보 기본 OFF(KIS 파수꾼이 실계좌 담당)")


def test_sell_alert_reenables():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ADVISOR_ALERTS"] = "1"
        try:
            advisor, sent = _run(tmp, flag=None)
            with mock.patch.object(advisor, "_quote", return_value=90.0):
                advisor.check_sell_alerts(
                    [{"code": "XX", "name": "테스트", "ccy": "USD",
                      "qty": 3, "avg": 100.0, "stop": 95.0, "target": 120.0}],
                    {}, dry_run=False)
            assert [m for m in sent if "손절" in m], "ADVISOR_ALERTS=1인데 미발송"
        finally:
            os.environ.pop("ADVISOR_ALERTS", None)
    print("[PASS] ADVISOR_ALERTS=1 → 페이퍼 매도 경보 발송(가역)")


def main():
    test_default_quiet()
    test_flag_reenables()
    test_sell_alert_gated_off_by_default()
    test_sell_alert_reenables()
    os.environ.pop("ADVISOR_SUGGEST_ALERTS", None)
    os.environ.pop("ADVISOR_ALERTS", None)
    print("\n제안·페이퍼매도 알림 OFF 검증 통과 — KIS 실계좌 매매만 알림.")


if __name__ == "__main__":
    main()
