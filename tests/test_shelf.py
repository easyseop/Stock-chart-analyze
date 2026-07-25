"""매물대 반등 슬리브(B) 신호 엔진 검증.

  1) 반등 확인: 매물대(VAL) 터치 후 상단마감·거래량·신저가아님 → ok
  2) 떨어지는 칼: 신저가 갱신·VAL 미회복 → 거부(falling-knife 방지)
  3) 손익비 부족·머리물량 과다·저점권 아님 → 거부
  4) gates.classify_shelf: 하드제외(폭등) 우선, shelf ok면 group=shelf

실행: python -m tests.test_shelf
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from scanner import analyze as A
from scanner import gates


def _df(lows, today_low, today_close, today_high, vol_bars=None):
    """마지막 봉=오늘. 20봉 이상 구성(신저가 판정용)."""
    n = max(len(lows), 22)
    lows = ([lows[0]] * (n - len(lows))) + lows
    close = [l + 1.0 for l in lows]
    high = [l + 1.5 for l in lows]
    op = [l + 0.5 for l in lows]
    vol = [1000.0] * n
    # 오늘 봉 덮어쓰기
    lows[-1] = today_low; close[-1] = today_close; high[-1] = today_high
    op[-1] = today_low + 0.2
    return pd.DataFrame({"Open": op, "High": high, "Low": lows,
                         "Close": close, "Volume": vol})


def _sup(val=98.0, vah=110.0, poc=100.0, price=101.0, overhead=0.3):
    return {"price": price, "long": {"val": val, "vah": vah},
            "long_poc": poc, "pnl": {"overhead": overhead}}


def test_bounce_ok():
    # 2봉 전 저가 98(VAL 터치), 오늘 저가 99.5·종가 101·고가 101.5(상단마감)
    lows = [100.0, 99.7, 98.0, 99.6, 99.5]
    d = _df(lows, today_low=99.5, today_close=101.0, today_high=101.5)
    r = A._shelf_signal(d, _sup(), {"mult": 1.5}, range_pos=0.30)
    assert r["ok"], r
    assert r["stop"] < r["entry"] < r["target"]
    assert r["rr"] >= 1.5
    print("[PASS] 반등 확인 → 매수 신호(진입<목표·손절<진입·RR≥1.5)")


def test_falling_knife_rejected():
    # 오늘이 신저가(97) + VAL(98) 미회복 종가 → 거부
    lows = [100.0, 99.0, 98.5, 98.2, 97.0]
    d = _df(lows, today_low=97.0, today_close=97.5, today_high=98.3)
    r = A._shelf_signal(d, _sup(), {"mult": 2.0}, range_pos=0.20)
    assert not r["ok"] and "반등 미확인" in r["reason"], r
    print("[PASS] 떨어지는 칼(신저가·VAL 미회복) → 거부")


def test_low_volume_rejected():
    lows = [100.0, 99.7, 98.0, 99.6, 99.5]
    d = _df(lows, today_low=99.5, today_close=101.0, today_high=101.5)
    r = A._shelf_signal(d, _sup(), {"mult": 0.8}, range_pos=0.30)   # 거래량 미달
    assert not r["ok"] and r["watch"] and "거래량" in r["reason"], r
    base = {"ccy": "USD", "turnover": 5e7, "shelf": r,
            "rs": {"rel": 0.0}, "ext": {"ma120_stretch": 0.0}}
    assert gates.classify_shelf_watch(base)["group"] == "shelf_watch"
    assert gates.classify_shelf(base)["group"] is None
    print("[PASS] 거래량 미동반 반등 → B 관찰만, 확정 B는 거부")


def test_watch_still_respects_hard_risk_limits():
    lows = [100.0, 99.0, 98.0, 97.0, 80.0]
    d = _df(lows, today_low=80.0, today_close=101.0, today_high=103.0)
    r = A._shelf_signal(
        d, _sup(val=80.0, vah=110.0), {"mult": 0.8}, range_pos=0.20)
    assert not r["ok"] and not r["watch"], r
    assert "손절폭 과대" in r["reason"], r
    print("[PASS] 반등 미확인이어도 손절폭 과대면 B 관찰에서 제외")


def test_overhead_and_zone():
    lows = [100.0, 99.7, 98.0, 99.6, 99.5]
    d = _df(lows, today_low=99.5, today_close=101.0, today_high=101.5)
    r1 = A._shelf_signal(d, _sup(overhead=0.8), {"mult": 1.5}, range_pos=0.30)
    assert not r1["ok"] and "머리 위 물량" in r1["reason"], r1
    r2 = A._shelf_signal(d, _sup(), {"mult": 1.5}, range_pos=0.80)  # 고점권
    assert not r2["ok"] and "저점권" in r2["reason"], r2
    print("[PASS] 머리 위 물량 과다 / 고점권 → 거부")


def test_classify_shelf():
    ok_shelf = {"ok": True, "entry": 101, "stop": 96, "target": 110, "reason": "매물대 지지 반등"}
    base = {"ccy": "USD", "turnover": 5e7, "shelf": ok_shelf,
            "rs": {"rel": 0.0}, "ext": {"ma120_stretch": 0.0}}
    assert gates.classify_shelf(base)["group"] == "shelf"
    # 이미 폭등이면 매물대 무관하게 제외
    blow = dict(base, rs={"rel": 0.5})
    assert gates.classify_shelf(blow)["group"] is None
    # shelf 미충족
    noshelf = dict(base, shelf={"ok": False, "reason": "반등 미확인"})
    assert gates.classify_shelf(noshelf)["group"] is None
    assert gates.classify_shelf_watch(noshelf)["group"] is None
    print("[PASS] classify_shelf: 하드제외 우선·확정/관찰 그룹 분리")


def test_partition_budget_isolation():
    """A/B 슬리브가 서로의 종목 수·투입원가를 세지 않아야(예산 잠식 방지)."""
    from bot import kis_buyloop as BL
    held_cost = {"AAA": 100.0, "BBB": 200.0, "CCC": 300.0}   # CCC=B, 나머지=A
    inflight = {"DDD": (50.0, "A"), "EEE": (60.0, "B")}
    b_codes = {"CCC"}
    na, ca = BL._partition(held_cost, inflight, "A", b_codes)
    nb, cb = BL._partition(held_cost, inflight, "B", b_codes)
    assert (na, ca) == (3, 350.0), (na, ca)   # AAA+BBB 보유 + DDD in-flight
    assert (nb, cb) == (2, 360.0), (nb, cb)   # CCC 보유 + EEE in-flight
    # shelf 후보 필터·정렬(RR 높은 순)
    sigs = [{"group": "now", "code": "X", "entry": 1, "stop": 0.9},
            {"group": "shelf_watch", "code": "W", "entry": 10, "stop": 9,
             "shelf": {"rr": 3.0}},
            {"group": "shelf", "code": "Y", "entry": 10, "stop": 9, "shelf": {"rr": 1.6}},
            {"group": "shelf", "code": "Z", "entry": 10, "stop": 9, "shelf": {"rr": 2.4}}]
    c = BL._shelf_cands(sigs)
    assert [x["code"] for x in c] == ["Z", "Y"], c
    print("[PASS] B 관찰은 매수루프에서 제외 + 확정 shelf만 필터/정렬")


def main():
    test_bounce_ok()
    test_falling_knife_rejected()
    test_low_volume_rejected()
    test_watch_still_respects_hard_risk_limits()
    test_overhead_and_zone()
    test_classify_shelf()
    test_partition_budget_isolation()
    print("\n매물대 반등 슬리브(B) 신호 엔진 통과 — 반등확인 매수·falling-knife 거부.")


if __name__ == "__main__":
    main()
