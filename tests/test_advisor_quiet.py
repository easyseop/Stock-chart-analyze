"""제안 알림 기본 OFF 검증 — 사용자 요청(2026-07-12).

  1) 기본(플래그 없음): 신선한 now 신호가 있어도 '매수 제안'·'도달' 텔레그램 0건
  2) ADVISOR_SUGGEST_ALERTS=1: 기존대로 매수 제안 발송(스위치 가역성)
  3) 매도(손절선 터치) 경보는 플래그와 무관하게 계속 발송(보호 알림 유지)

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


def test_sell_alert_unaffected():
    with tempfile.TemporaryDirectory() as tmp:
        advisor, sent = _run(tmp, flag=None)
        with mock.patch.object(advisor, "_quote", return_value=90.0):
            n = advisor.check_sell_alerts(
                [{"code": "XX", "name": "테스트", "ccy": "USD",
                  "qty": 3, "avg": 100.0, "stop": 95.0, "target": 120.0}],
                {}, dry_run=False)
        assert n == 1
        assert [m for m in sent if "손절" in m], "손절 경보가 억제됨(보호 알림 훼손)"
    print("[PASS] 보유 손절선 터치 경보는 플래그와 무관하게 발송")


def main():
    test_default_quiet()
    test_flag_reenables()
    test_sell_alert_unaffected()
    os.environ.pop("ADVISOR_SUGGEST_ALERTS", None)
    print("\n제안 알림 OFF 스위치 검증 통과 — 액션/보호 알림만 유지.")


if __name__ == "__main__":
    main()
