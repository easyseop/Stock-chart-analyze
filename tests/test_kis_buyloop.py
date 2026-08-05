"""매수 루프(Loop B) — 신선한 스캐너 신호의 KIS 직접 집행 검증(모킹).

브로커-진실: 이미 KIS 보유(3거래소 병합)·잔고 불명·가격 괴리·장외·어닝 D-3·
당일 매도 쿨다운은 execute_entry 호출 전 skip. 게이트 통과분만 execute_entry로,
브로커-진실 입력(open_positions·open_cost_krw)이 사이클 내에서 누적된다.
mirror의 동시 보유 수는 제한하지 않고 open_cost 기반 예산 게이트를 사용한다.

실행: python -m tests.test_kis_buyloop
"""
from __future__ import annotations

import datetime
import math
import os
import json
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kis_buyloop as BL, kis_buy  # noqa: E402


def _sig(code="005930", ccy="KRW", entry=100.0, stop=95.0,
         group="now", fresh=True, **kw):
    sig = {"group": group, "id": f"s-{code}", "code": code, "name": "t",
           "ccy": ccy, "entry": entry, "stop": stop, "fresh": fresh,
           "stage": 3, "norm": 50, **kw}
    # B(shelf)는 target이 필수 계약(finite > entry) — 명시하지 않은 테스트
    # 픽스처에 기본 유효 목표가를 채운다(entry가 숫자가 아닌 반례는 그대로).
    if group == "shelf" and "target" not in kw:
        try:
            sig["target"] = float(entry) * 1.1
        except (TypeError, ValueError):
            pass
    if group == "shelf" and "shelf" not in kw:
        try:
            e, st, tg = float(entry), float(stop), float(sig["target"])
            sig["shelf"] = {"rr": (tg - e) / (e - st)}
        except (TypeError, ValueError, ZeroDivisionError, KeyError):
            sig["shelf"] = {"rr": 2.0}
    return sig


def _rows_of(holdings, market, excg):
    """{code: qty} → positions_detail 정규화 행(기본: US 보유는 NASD에만)."""
    rows = []
    for c, q in (holdings or {}).items():
        mk = "KR" if (len(c) == 6 and c[:5].isdigit()) else "US"
        if mk != market or (market == "US" and excg != "NASD"):
            continue
        rows.append({"code": c, "name": c, "qty": q, "avg": 100.0, "cur": 100.0,
                     "eval_amt": 100.0 * q, "buy_amt": 100.0 * q, "pl_amt": 0.0,
                     "pl_rt": 0.0, "ccy": "KRW" if mk == "KR" else "USD",
                     "market": mk})
    return rows


def _run(signals, holdings=None, last=100.0, exec_ret=None, mkt_open=True,
         fold=None, pd=None, run_kwargs=None):
    """holdings: {code: qty}|None(None=잔고 조회실패). pd: positions_detail 대체.

    autopaper mock이 없다 — KIS 매수 경로는 scanner 신호와 KIS 상태만 본다
    (2026-08-05 정정). 네트워크 호출이 생기면 _no_network가 즉시 실패시킨다.
    """
    if exec_ret is None:
        exec_ret = kis_buy.BuyDecision(True, "sent", "ack ODNO=1", qty=3)

    def _no_network(*a, **k):
        raise AssertionError("buyloop이 외부 HTTP를 호출함 — autopaper 의존 금지")

    def fake_pd(market="US", excg="NASD"):
        if holdings is None:
            return None
        return _rows_of(holdings, market, excg)

    tf = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False); tf.close()
    lf = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False); lf.close()
    with mock.patch("urllib.request.urlopen", side_effect=_no_network), \
         mock.patch.object(BL.kis, "positions_detail", side_effect=pd or fake_pd), \
         mock.patch.object(BL.kis, "last_price", return_value=last), \
         mock.patch.object(BL.settings, "market_open", return_value=mkt_open), \
         mock.patch.object(BL.kis_positions, "PATH", tf.name), \
         mock.patch("bot.ledger.LEDGER_PATH", lf.name), \
         mock.patch("bot.ownership.baseline", return_value=set()), \
         mock.patch("bot.ledger._fold", return_value=fold or {}), \
         mock.patch.object(BL.kis_buy, "execute_entry",
                           return_value=exec_ret) as ex:
        res = BL.run_once(signals, **(run_kwargs or {}))
        recorded = BL.kis_positions.load()
        with open(lf.name, encoding="utf-8") as f:
            ledger_events = [json.loads(x) for x in f if x.strip()]
    os.unlink(tf.name); os.unlink(lf.name)
    return res, ex, recorded, ledger_events


def _g(res, code):
    return next((r for r in res if r["code"] == code), None)


def test_happy_path_executes():
    res, ex, rec, _ = _run([_sig()], holdings={})
    r = _g(res, "005930")
    assert r["ok"] and r["gate"] == "sent" and r["qty"] == 3
    assert ex.called
    kw = ex.call_args.kwargs
    assert kw.get("market") == "KR"
    # 브로커-진실 캡 입력 전달: 빈 계좌 → 0/0. ack 대사 기준 hldg_before=0.
    assert kw.get("open_positions") == 0 and kw.get("open_cost_krw") == 0
    assert kw.get("hldg_before") == 0
    # ack는 체결이 아니므로 보호 포지션을 미리 만들지 않는다.
    assert rec == {}
    print("[PASS] now·미보유·가격근접 → 주문 접수, ack 단계 포지션 미기록")


def test_already_held_skips():
    res, ex, rec, _ = _run([_sig()], holdings={"005930": 5})
    assert _g(res, "005930")["gate"] == "already" and not ex.called
    print("[PASS] 이미 KIS 보유 → skip(중복매수 금지)")


def test_nyse_holding_merged():
    """NYSE에만 있는 보유가 병합 조회로 잡혀 중복매수를 막는지(검토 구멍 수정)."""
    def pd(market="US", excg="NASD"):
        if market == "US" and excg == "NYSE":
            return [{"code": "KKR", "name": "KKR", "qty": 2, "avg": 90.0,
                     "cur": 90.0, "eval_amt": 180.0, "buy_amt": 180.0,
                     "pl_amt": 0.0, "pl_rt": 0.0, "ccy": "USD", "market": "US"}]
        return []
    res, ex, rec, _ = _run([_sig(code="KKR", ccy="USD", entry=90.0, stop=85.0)],
                        holdings={}, pd=pd, last=90.0)
    assert _g(res, "KKR")["gate"] == "already" and not ex.called
    print("[PASS] NYSE 보유 병합 → 중복매수 차단(NASD-only 구멍 수정)")


def test_holdings_unknown_skips():
    res, ex, rec, _ = _run([_sig()], holdings=None)          # 잔고 조회 실패
    assert _g(res, "005930")["gate"] == "holdings" and not ex.called
    print("[PASS] 잔고 조회실패 → 보수적 전면 skip")


def test_price_deviation_skips():
    res, ex, rec, _ = _run([_sig(entry=100.0)], holdings={}, last=110.0)  # +10% 이탈
    assert _g(res, "005930")["gate"] == "tolerance" and not ex.called
    print("[PASS] 가격 괴리(진입가 ±1.5% 밖) → skip")


def test_market_closed_skips():
    res, ex, rec, _ = _run([_sig()], holdings={}, mkt_open=False)
    assert _g(res, "005930")["gate"] == "session" and not ex.called
    print("[PASS] 장외 → skip")


def test_earnings_window_skips():
    res, ex, rec, _ = _run([_sig(earnings_d=2)], holdings={})
    assert _g(res, "005930")["gate"] == "earnings" and not ex.called
    res2, ex2, _, _ = _run([_sig(earnings_d=5)], holdings={})
    assert _g(res2, "005930")["ok"]                       # D-5는 통과
    print("[PASS] 어닝 D-3 이내 → skip (autopaper 패리티)")


def test_cooldown_after_same_day_sell():
    import time as _t
    fold = {"x#1": {"symbol": "005930", "side": "SELL", "state": "filled",
                    "submitted_at": _t.time(), "intended": 3, "filled": 3}}
    res, ex, rec, _ = _run([_sig()], holdings={}, fold=fold)
    assert _g(res, "005930")["gate"] == "cooldown" and not ex.called
    print("[PASS] 당일 매도 종목 재진입 금지(쿨다운 — autopaper 패리티)")


def test_inflight_buy_counts_toward_position_accounting():
    import time as _t
    fold = {"kb:y#1": {"symbol": "TSLA", "side": "BUY", "state": "ack",
                       "submitted_at": _t.time(), "intended": 2, "filled": 0}}
    res, ex, rec, _ = _run([_sig()], holdings={}, fold=fold)
    assert ex.call_args.kwargs.get("open_positions") == 1   # in-flight BUY 가산
    print("[PASS] in-flight BUY도 포지션 현황에 가산(하위 Stage·진단용)")


def test_b_sleeve_has_no_fixed_position_count_gate():
    """B 예약이 25종목이어도 buyloop가 고정 개수로 막지 않고 예산 게이트에 위임."""
    import time as _t
    fold = {
        f"sb:planned:{i}": {
            "symbol": f"{i:06d}", "side": "BUY", "state": "planned",
            "created_at": _t.time() + i, "intended": 1, "filled": 0,
            "price": 10.0, "market": "KR", "sleeve": "B",
            "pos_key": f"sb:planned:{i}",
        }
        for i in range(25)
    }
    sig = _sig(code="999999", group="shelf", entry=100.0, stop=95.0)
    res, ex, _, _ = _run(
        [sig], holdings={}, fold=fold,
        run_kwargs={"sleeve": "B", "group": "shelf", "seed_krw": 5_000_000})
    assert ex.called and _g(res, "999999")["ok"]
    assert ex.call_args.kwargs["open_positions"] == 25
    assert ex.call_args.kwargs["open_cost_krw"] == 250.0
    print("[PASS] B 25종목 예약 상태도 고정 개수 차단 없이 예산 게이트로 전달")


def test_a_fresh_signal_executes_without_autopaper():
    """지시 §10-1: paper 피드가 죽어 있어도(모든 HTTP 차단) A 신호는 진행된다."""
    res, ex, _, _ = _run([_sig()], holdings={})
    assert _g(res, "005930")["gate"] == "sent" and ex.call_count == 1
    print("[PASS] autopaper 없이 신선한 A 신호가 KIS 게이트까지 전달")


def test_no_autopaper_network_call_in_buyloop():
    """지시 §10-2: run_once가 외부 HTTP를 한 번이라도 부르면 즉시 실패.

    _run 하네스가 urllib.request.urlopen을 AssertionError로 덮으므로, A·B 두
    경로 모두 네트워크 없이 완주해야 한다.
    """
    sig_b = _sig(code="000660", group="shelf", entry=100.0, stop=95.0)
    sig_b["shelf"] = {"rr": 2.0}
    res, ex, _, _ = _run([_sig(), sig_b], holdings={})
    assert _g(res, "005930")["gate"] == "sent"
    res_b, ex_b, _, _ = _run([sig_b], holdings={},
                             run_kwargs={"sleeve": "B", "group": "shelf",
                                         "seed_krw": 5_000_000})
    assert _g(res_b, "000660")["ok"]
    print("[PASS] 매수 경로에 외부 HTTP 0회(autopaper 의존 없음) — A·B 모두")


def test_a_outside_tolerance_then_inside_executes_once():
    """지시 §10-4·5: tolerance 밖이면 그 사이클 skip, 들어오면 정확히 1회 주문."""
    res1, ex1, _, _ = _run([_sig(entry=100.0)], holdings={}, last=102.0)
    assert _g(res1, "005930")["gate"] == "tolerance" and not ex1.called
    res2, ex2, _, _ = _run([_sig(entry=100.0)], holdings={}, last=100.4)
    assert _g(res2, "005930")["gate"] == "sent" and ex2.call_count == 1
    print("[PASS] 진입가 관찰 — 괴리 skip 후 조건 도달 시 1회 주문")


def test_malformed_signal_is_fail_closed():
    """지시 §9-5: 계약 위반 후보는 후보 선정 단계에서 제외(주문 0)."""
    bad = [
        {"group": "now", "code": "AAA", "fresh": True},           # entry/stop 없음
        {"group": "now", "code": "BBB", "fresh": False,
         "entry": 100.0, "stop": 95.0},                           # 신선하지 않음
        {"group": "watch", "code": "CCC", "fresh": True,
         "entry": 100.0, "stop": 95.0},                           # now 아님
        {"group": "now", "code": "DDD", "fresh": True,
         "entry": 0, "stop": 95.0},                               # entry=0
    ]
    res, ex, _, _ = _run(bad, holdings={})
    assert not ex.called and all(r.get("gate") != "sent" for r in res)
    print("[PASS] 손상·비신선·비now·entry=0 후보는 전부 주문 0")


def test_no_order_on_nan_zero_negative_or_inverted_stop():
    """지시 §9-16 + Codex V2 P1: 0·음수·NaN·inf·역전 stop은 A/B 모두 주문 0.

    음수 stop은 per_share가 오히려 큰 양수(100-(-5)=105)가 되어 sent까지
    갔었다 — 체결되면 회계·보호원장이 stop<=0을 거부해 무보호 실보유가 남는다.
    """
    bad_stops = (0.0, -0.01, -5.0, float("-inf"), float("nan"),
                 100.0,                              # stop == entry
                 120.0)                              # stop > entry(역전)
    for stop in bad_stops:
        for group, sleeve, extra in (("now", "A", {}),
                                     ("shelf", "B", {"shelf": {"rr": 2.0}})):
            sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=stop,
                       group=group, **extra)
            res, ex, _, _ = _run(
                [sig], holdings={},
                run_kwargs={"sleeve": sleeve, "group": group})
            assert not ex.called, (stop, group)
            # 조용히 사라지면 안 된다 — 후보에 남아 **명시적 input 진단**을
            #   남겨야 한다(Codex V3 P2: stop=0이 truthy 필터에서 무진단 소멸).
            r = _g(res, "AAPL")
            assert r is not None and r["gate"] == "input", (stop, group, res)
    # 양성: 유효한 0 < stop < order_px는 기존대로 실행기까지 전달된다.
    res, ex, _, _ = _run([_sig(entry=100.0, stop=95.0)], holdings={})
    assert ex.called and _g(res, "005930")["gate"] == "sent"
    print("[PASS] 0·음수·NaN·inf·역전 손절은 A/B 주문 0 · 유효 stop만 전달")


def test_a_has_no_fixed_position_count_cap():
    """지시 §9-11: A 보유가 12개+여도 개수만으로 후보 검토를 멈추지 않는다.

    실제 투입 한도는 execute_entry의 예산·매수여력 게이트가 결정한다(모킹).
    """
    held = {f"H{i:03d}": 1 for i in range(14)}
    sig = _sig(code="NEWCO", ccy="USD", entry=100.0, stop=95.0)
    res, ex, _, _ = _run([sig], holdings=held, last=100.0)
    assert _g(res, "NEWCO")["gate"] == "sent"
    assert ex.call_args.kwargs["open_positions"] == 14   # 개수는 전달만, 차단 없음
    print("[PASS] A 고정 종목 수 상한 없음 — 예산 게이트에 위임")


def test_non_boolean_fresh_is_rejected_for_a_too():
    """freshness 계약은 `fresh is True`(엄격) — truthy 문자열("yes"·"1")이
    통과하면 손상 문서의 비-bool 값이 신선 판정을 위조한다. A/B 동일."""
    for fake in ("yes", "1", 1, ["x"]):
        for group, sleeve, extra in (("now", "A", {}),
                                     ("shelf", "B", {"shelf": {"rr": 2.0}})):
            sig = _sig(code="AAPL", ccy="USD", group=group,
                       fresh=fake, **extra)
            res, ex, _, _ = _run([sig], holdings={},
                                 run_kwargs={"sleeve": sleeve, "group": group})
            assert not ex.called, (fake, group)
    print("[PASS] truthy 비-bool fresh는 A/B 모두 후보 제외(is True 엄격)")


def test_b_stale_row_in_fresh_document_is_rejected():
    """Codex P1-2 재현: 문서 자체는 신선해도 shelf 행이 fresh=False(또는 필드
    없음)이면 실행기로 넘어가면 안 된다 — '신선한 신호만 집행' 전제."""
    stale = _sig(code="TSLA", ccy="USD", group="shelf", fresh=False,
                 shelf={"rr": 2.0})
    nofield = _sig(code="NVDA", ccy="USD", group="shelf", shelf={"rr": 2.0})
    del nofield["fresh"]
    ok = _sig(code="AAPL", ccy="USD", group="shelf", fresh=True,
              shelf={"rr": 2.0})
    res, ex, _, _ = _run([stale, nofield, ok], holdings={},
                         run_kwargs={"sleeve": "B", "group": "shelf"})
    assert ex.call_count == 1, [c.args[1] for c in ex.call_args_list]
    assert ex.call_args.args[1] == "AAPL"          # fresh=True만 실행기 도달
    assert _g(res, "TSLA") is None and _g(res, "NVDA") is None
    print("[PASS] B stale 행(fresh=False·필드 없음)은 실행기 미도달 — fresh만")


def test_nan_quote_is_gated_without_crash():
    """Codex P2 재현: NaN 현재가는 `not cur`도 `cur<=0`도 False — 예외 없이
    quote 게이트에서 끝나야 한다(사이클 중단 금지)."""
    for bad in (float("nan"), float("inf")):
        res, ex, _, _ = _run([_sig()], holdings={}, last=bad)
        assert _g(res, "005930")["gate"] == "quote", bad
        assert not ex.called
    print("[PASS] NaN·inf 현재가 → 예외 0·실행기 호출 0(quote 게이트)")


def test_corrupt_target_is_rejected_not_persisted():
    """Codex V2 P2 + V5 P1-2: 오염 목표가는 저장은커녕 **주문 자체를 차단**한다
    (NaN이 메타로 전파되면 목표청산 비교가 조용히 꺼지고, entry 이하 값은 매수
    직후 전량 목표매도를 발동). 명시 오염값은 gate=input."""
    # entry와 정확히 같은 값(100.0)도 A에서 차단 — `<=`를 `<`로 약화하는
    # mutation(M3)을 A 경로에서 잡는다(B는 runtime 발주가 게이트가 마스킹).
    for bad in (float("nan"), float("inf"), -1.0, "x", True, 100.0, 99.0):
        res, ex, _, _ = _run([_sig(target=bad)], holdings={})
        assert not ex.called, bad
        assert _g(res, "005930")["gate"] == "input", (bad, res)
    # legacy 호환: target 부재·0(목표 없음)은 A에서 None으로 진행.
    for absent in ("omit", 0):
        sig = _sig()
        if absent == "omit":
            sig.pop("target", None)
        else:
            sig["target"] = 0
        res, ex, _, _ = _run([sig], holdings={})
        assert ex.called and ex.call_args.kwargs["order_meta"]["target"] is None
    res2, ex2, _, _ = _run([_sig(target=110.0)], holdings={})
    assert ex2.call_args.kwargs["order_meta"]["target"] == 110.0  # 유효값 보존
    print("[PASS] 오염 목표가는 주문 차단 · 부재/0은 legacy None · 유효값 보존")


def test_invalid_fx_fails_closed_for_all_candidates():
    """Codex V2 P2: 명시적 fx=0이 `fx or 기본값`으로 되살아나 낡은 기본 환율로
    사이징하던 문제 — None만 기본값, 전달된 값은 그대로 검증한다."""
    for bad in (float("nan"), float("inf"), 0, -1, "invalid"):
        res, ex, _, _ = _run([_sig(code="AAPL", ccy="USD")], holdings={},
                             run_kwargs={"fx": bad})
        assert not ex.called, bad
        assert res and res[0]["gate"] == "input" and "환율" in res[0]["why"], bad
    # fx=None(기본값 사용)·유효 양수는 그대로 실행기 도달.
    res, ex, _, _ = _run([_sig(code="AAPL", ccy="USD")], holdings={},
                         run_kwargs={"fx": None})
    assert ex.called
    res2, ex2, _, _ = _run([_sig(code="AAPL", ccy="USD")], holdings={},
                           run_kwargs={"fx": 1400.0})
    assert ex2.called and ex2.call_args.kwargs["krw_per_usd"] == 1400.0
    print("[PASS] 0·음수·NaN·inf·비숫자 환율 fail-closed · None만 기본값")


def test_unknown_tactic_never_bypasses_tolerance():
    """Codex V3 P1 재현: tactic.mode='banana'가 full/half tolerance와 눌림가
    검사를 전부 우회해 entry에서 100% 이탈한 현재가로 sent까지 갔다."""
    for bad in ("banana", "unknown", ["x"], {"weird": 1}):
        for group, sleeve, extra in (("now", "A", {}),
                                     ("shelf", "B", {"shelf": {"rr": 2.0}})):
            sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
                       group=group, tactic={"mode": bad}, **extra)
            res, ex, _, _ = _run(
                [sig], holdings={}, last=200.0,      # entry 대비 +100% 이탈
                run_kwargs={"sleeve": sleeve, "group": group})
            assert not ex.called, (bad, group)
            r = _g(res, "AAPL")
            assert r is not None and r["gate"] == "tactic", (bad, group, res)
    # 대소문자·공백 변형은 정상 full로 정규화 → tolerance가 적용된다.
    for variant in ("FULL", " full ", "Full"):
        res, ex, _, _ = _run(
            [_sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
                  tactic={"mode": variant})], holdings={}, last=200.0)
        assert not ex.called, variant
        assert _g(res, "AAPL")["gate"] == "tolerance", (variant, res)
        res2, ex2, _, _ = _run(
            [_sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
                  tactic={"mode": variant})], holdings={}, last=100.4)
        assert ex2.called and _g(res2, "AAPL")["gate"] == "sent", variant
    # 정규화가 pullback 계약도 보존: 대문자 PULLBACK + 유효 pb → 지정가 경로.
    res3, ex3, _, _ = _run(
        [_sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
              tactic={"mode": "PULLBACK", "pb_price": 97.0})],
        holdings={}, last=200.0)                     # pullback은 tolerance 무관
    assert ex3.called and ex3.call_args.kwargs["price_usd"] == 97.0
    print("[PASS] 알 수 없는 전술 차단·변형 정규화 — tolerance 우회 경로 없음")


def test_falsy_tactic_values_do_not_default_to_full():
    """Codex V4 P1-1 재현: `raw or "full"` 한 줄이 []·{}·0·False·""를 전부
    정상 full 주문으로 둔갑시켰다(tolerance 안이면 sent). 명시된 위반값은
    gate=tactic, **필드가 정말 없을 때만** legacy full이어야 한다."""
    falsy_modes = ([], {}, 0, False, None, "", "   ")
    for bad in falsy_modes:
        for group, sleeve, extra in (("now", "A", {}),
                                     ("shelf", "B", {"shelf": {"rr": 2.0}})):
            sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
                       group=group, tactic={"mode": bad}, **extra)
            res, ex, _, _ = _run(
                [sig], holdings={}, last=100.4,      # tolerance 안 — 종전엔 sent
                run_kwargs={"sleeve": sleeve, "group": group})
            assert not ex.called, (bad, group)
            r = _g(res, "AAPL")
            assert r is not None and r["gate"] == "tactic", (bad, group, res)
    # tactic 자체가 falsy 구조로 온 경우도 동일(종전 `or {}`가 정상화했음).
    for bad_tactic in ([], 0, False):
        sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
                   tactic=bad_tactic)
        res, ex, _, _ = _run([sig], holdings={}, last=100.4)
        assert not ex.called, bad_tactic
        assert _g(res, "AAPL")["gate"] == "tactic", (bad_tactic, res)
    # legacy 호환은 **부재**에만: tactic 필드 없음·tactic=None·dict에 mode 키
    #   없음 → full로 정상 진행. 문자열 전술("full")도 종전대로 허용.
    for legacy in (None, {}, "full"):
        sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0)
        if legacy is None:
            sig.pop("tactic", None)
        else:
            sig["tactic"] = legacy
        res, ex, _, _ = _run([sig], holdings={}, last=100.4)
        assert ex.called and _g(res, "AAPL")["gate"] == "sent", legacy
    print("[PASS] falsy 전술값은 full 둔갑 없이 차단 · 부재만 legacy full")


def test_stop_at_or_above_entry_blocked_regardless_of_price():
    """Codex V4 P1-2 재현: full의 order_px는 실시간 현재가라, cur이 entry보다
    조금 높으면 stop==entry·소폭 역전이 per_share>0으로 통과해 sent까지 갔다
    (극소 손절폭 → 위험기반 수량 폭증 방향). 신호 불변식 stop<entry를
    현재가와 무관하게 강제한다."""
    for stop in (100.0, 100.0001, 100.2, 120.0):
        for cur in (100.0, 100.1, 100.4, 101.49):    # 전부 tolerance 안
            for group, sleeve, extra in (("now", "A", {}),
                                         ("shelf", "B", {"shelf": {"rr": 2.0}})):
                sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=stop,
                           group=group, **extra)
                res, ex, _, _ = _run(
                    [sig], holdings={}, last=cur,
                    run_kwargs={"sleeve": sleeve, "group": group})
                assert not ex.called, (stop, cur, group)
                r = _g(res, "AAPL")
                assert r is not None and r["gate"] == "input", \
                    (stop, cur, group, res)
    # 유효 경계 0 < stop < entry는 기존대로 통과.
    res, ex, _, _ = _run([_sig(code="AAPL", ccy="USD",
                               entry=100.0, stop=99.9)],
                         holdings={}, last=100.4)
    assert ex.called and _g(res, "AAPL")["gate"] == "sent"
    print("[PASS] stop>=entry는 현재가와 무관하게 input 차단 · 유효 stop 통과")


def test_no_entry_when_price_at_or_below_stop():
    """Codex V5 P1-1 재현: 손절선을 이미 깬 현재가에서 pullback 지정가(pb>cur)
    가 시장성 주문이 되어 붕괴 종목을 즉시 매수했다. 전술 무관 실시간 무효선:
    cur <= stop이면 신규 주문 0."""
    pb_tac = {"mode": "pullback", "pb_price": 97.0}
    for cur, expect_sent in ((95.01, True), (95.0, False), (94.9, False)):
        for group, sleeve, extra in (("now", "A", {}),
                                     ("shelf", "B", {"shelf": {"rr": 2.0}})):
            sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
                       group=group, tactic=dict(pb_tac), **extra)
            res, ex, _, _ = _run(
                [sig], holdings={}, last=cur,
                run_kwargs={"sleeve": sleeve, "group": group})
            if expect_sent:                      # stop 위 — pb 상한 지정가 유지
                assert ex.called, (cur, group)
                assert ex.call_args.kwargs["price_usd"] == 97.0
            else:
                assert not ex.called, (cur, group)
                assert _g(res, "AAPL")["gate"] == "tactic", (cur, group, res)
    # full도 동일 — stop이 entry에 붙어 있으면 tolerance 안에서도 이탈 가능.
    res, ex, _, _ = _run([_sig(code="AAPL", ccy="USD",
                               entry=100.0, stop=99.5)],
                         holdings={}, last=99.4)
    assert not ex.called and _g(res, "AAPL")["gate"] == "tactic"
    print("[PASS] cur<=stop이면 전술 무관 신규 주문 0 · stop 위 눌림은 유지")


def test_b_requires_valid_target_above_entry():
    """Codex V5 P1-2 재현: B target<=entry가 저장되면 매수 직후 전량 목표매도
    발동, 오염 target을 None으로 눙치면 VAH 목표청산 자체가 사라졌다.
    B는 finite target > entry 필수."""
    for bad in (None, 0, -1, float("nan"), float("inf"),
                99.0, 100.0, "x", True):
        sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
                   group="shelf", shelf={"rr": 2.0}, target=bad)
        res, ex, _, _ = _run([sig], holdings={}, last=100.4,
                             run_kwargs={"sleeve": "B", "group": "shelf"})
        assert not ex.called, bad
        assert _g(res, "AAPL")["gate"] == "input", (bad, res)
    # target 필드 자체가 없어도 B는 필수 위반.
    sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
               group="shelf", shelf={"rr": 2.0}, target=None)
    ok = _sig(code="MSFT", ccy="USD", entry=100.0, stop=95.0,
              group="shelf", shelf={"rr": 2.0}, target=110.0)
    res, ex, _, _ = _run([sig, ok], holdings={}, last=100.4,
                         run_kwargs={"sleeve": "B", "group": "shelf"})
    assert ex.call_count == 1 and ex.call_args.args[1] == "MSFT"
    assert ex.call_args.kwargs["order_meta"]["target"] == 110.0
    print("[PASS] B는 finite target>entry 필수 — 오염값 주문 0·유효값만 전달")


def test_b_target_must_exceed_actual_order_price():
    """Codex V6 P1-1 재현: target>entry여도 **실제 발주가** 이하면 체결 직후
    decide_b(price>=target)가 즉시 전량 목표매도를 발동한다. 계약은
    `target > order_px`(full/half=현재가, pullback=눌림가) — 임의 최소 간격이
    아니라 매수 직후 청산 금지의 최소 의미."""
    from bot import kis_exits
    today = datetime.date.today().isoformat()
    # full: 실제 order_px는 현재가+30bp 마켓터블 지정가. 즉시매도만 피하고
    # 1.5R 미만인 목표도 모두 차단한다.
    for target in (100.0001, 100.4, 100.4001, 101.0, 107.9):
        for cur in (99.9, 100.0, 100.4):
            sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
                       group="shelf", target=target)
            res, ex, _, _ = _run([sig], holdings={}, last=cur,
                                 run_kwargs={"sleeve": "B", "group": "shelf"})
            assert not ex.called, (target, cur, res)
            assert _g(res, "AAPL")["gate"] == "input", (target, cur, res)
    # 정상 2R 신호는 실제 마켓터블 상한에서도 1.5R 이상이면 허용.
    sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
               group="shelf", target=110.0)
    res, ex, _, _ = _run([sig], holdings={}, last=100.4,
                         run_kwargs={"sleeve": "B", "group": "shelf"})
    assert ex.called, res
    order_px = ex.call_args.kwargs["price_usd"]
    assert order_px == 100.7
    assert (110.0 - order_px) / (order_px - 95.0) >= 1.5
    assert kis_exits.decide_b(110.0, order_px, 3, today, today) == []
    # pullback도 신호 시점·실제 pb 시점 양쪽 계약을 만족해야 허용.
    for target, expect_gate in ((97.0, "input"),
                                (100.0001, "input"),
                                (110.0, "sent")):
        sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
                   group="shelf", target=target,
                   tactic={"mode": "pullback", "pb_price": 97.0})
        res, ex, _, _ = _run([sig], holdings={}, last=98.0,
                             run_kwargs={"sleeve": "B", "group": "shelf"})
        if expect_gate == "sent":
            assert ex.called and ex.call_args.kwargs["price_usd"] == 97.0
            assert kis_exits.decide_b(target, 97.0, 3, today, today) == []
        else:
            assert not ex.called, (target, res)
            assert _g(res, "AAPL")["gate"] == expect_gate, (target, res)
    print("[PASS] B target·실제RR·손절폭은 마켓터블 발주가 기준 계약 준수")


def test_corrupt_priority_fields_reject_row_not_cycle():
    """Codex V6 P2-1 재현: stage=[]·shelf.rr={dict} 같은 구조값이 원본 dict
    정렬에서 TypeError를 일으켜 **사이클 전체**가 죽고 정상 후보까지 처리
    불가였다. 이제 정렬은 검증 후 vc 필드로만 하고, 오염 행은 행 단위
    gate=input, 정상 형제 행은 계속 처리된다."""
    bad_a = _sig(code="AAPL", ccy="USD", stage=[])
    ok_a = _sig(code="MSFT", ccy="USD", stage=3)
    res, ex, _, _ = _run([bad_a, ok_a], holdings={})
    assert ex.call_count == 1 and ex.call_args.args[1] == "MSFT"
    assert _g(res, "AAPL")["gate"] == "input"
    bad_norm = _sig(code="AAPL", ccy="USD", norm={"bad": 1})
    res, ex, _, _ = _run([bad_norm], holdings={})
    assert not ex.called and _g(res, "AAPL")["gate"] == "input"
    bad_b = _sig(code="AAPL", ccy="USD", group="shelf",
                 shelf={"rr": {"bad": 1}}, target=110.0)
    ok_b = _sig(code="MSFT", ccy="USD", group="shelf",
                shelf={"rr": 2.0}, target=110.0)
    res, ex, _, _ = _run([bad_b, ok_b], holdings={},
                         run_kwargs={"sleeve": "B", "group": "shelf"})
    assert ex.call_count == 1 and ex.call_args.args[1] == "MSFT"
    assert _g(res, "AAPL")["gate"] == "input"
    for bad_shelf in ([], "bad", False, None):
        sig = _sig(code="AAPL", ccy="USD", group="shelf",
                   shelf=bad_shelf, target=110.0)
        res, ex, _, _ = _run([sig], holdings={},
                             run_kwargs={"sleeve": "B", "group": "shelf"})
        assert not ex.called and _g(res, "AAPL")["gate"] == "input"
    # 부재/None은 기본값 0으로 정상 진행(legacy 호환).
    absent = _sig(code="AAPL", ccy="USD")
    absent.pop("stage", None); absent.pop("norm", None)
    res, ex, _, _ = _run([absent], holdings={})
    assert ex.called
    print("[PASS] 오염 우선순위 필드는 행 거부·사이클 생존 — 정렬은 검증 후")


def test_b_actual_rr_and_stop_width_boundaries():
    """B의 승인된 1.5R·15% 계약을 실제 마켓터블 발주가로 재검증."""
    cur, stop = 100.0, 95.0
    order_px = BL.kis_orders.marketable_limit_price(cur, "BUY", market="US")
    exact_target = order_px + BL.settings.SHELF_MIN_RR * (order_px - stop)
    for delta, sent in ((-1e-6, False), (0.0, True), (1e-6, True)):
        target = exact_target + delta
        sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=stop,
                   group="shelf", target=target)
        res, ex, _, _ = _run([sig], holdings={}, last=cur,
                             run_kwargs={"sleeve": "B", "group": "shelf"})
        assert ex.called is sent, (delta, res)
    exact_stop = order_px * (1.0 - BL.settings.SHELF_MAX_STOP)
    for delta, sent in ((-1e-6, False), (0.0, True), (1e-6, True)):
        st = exact_stop + delta
        target = order_px + 2.0 * (order_px - st)
        sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=st,
                   group="shelf", target=target)
        res, ex, _, _ = _run([sig], holdings={}, last=cur,
                             run_kwargs={"sleeve": "B", "group": "shelf"})
        assert ex.called is sent, (delta, res)
    # 메타 RR가 가격과 다르면, 숫자 자체가 그럴듯해도 거부한다.
    sig = _sig(code="AAPL", ccy="USD", entry=100.0, stop=95.0,
               group="shelf", target=110.0, shelf={"rr": 9.9})
    res, ex, _, _ = _run([sig], holdings={}, last=100.0,
                         run_kwargs={"sleeve": "B", "group": "shelf"})
    assert not ex.called and _g(res, "AAPL")["gate"] == "input"
    print("[PASS] B 실제 1.5R·15% 경계와 rr 메타 일치 계약")


def test_signal_identity_name_and_call_contract():
    """원장 키 충돌·HTML 알림 오염·잘못된 내부 sleeve/group 호출 차단."""
    for bad_id in (True, 7, "", "has space", "bad/slash", "x" * 129):
        sig = _sig(code="AAPL", ccy="USD", id=bad_id)
        res, ex, _, _ = _run([sig], holdings={})
        assert not ex.called and _g(res, "AAPL")["gate"] == "input", bad_id
    for bad_name in (True, {"x": 1}, "x" * 121):
        sig = _sig(code="AAPL", ccy="USD", name=bad_name)
        res, ex, _, _ = _run([sig], holdings={})
        assert not ex.called and _g(res, "AAPL")["gate"] == "input", bad_name
    one = _sig(code="AAPL", ccy="USD", id="same-id")
    two = _sig(code="MSFT", ccy="USD", id="same-id")
    _, ex, _, _ = _run([one, two], holdings={})
    keys = [call.args[0] for call in ex.call_args_list]
    assert keys == ["kb:AAPL:same-id", "kb:MSFT:same-id"]
    with mock.patch("bot.notify.send") as send:
        sig = _sig(code="AAPL", ccy="USD", name="AT&T <bad>")
        _, ex, _, _ = _run([sig], holdings={})
    assert ex.called and send.call_count == 1
    text = send.call_args.args[0]
    assert "AT&amp;T &lt;bad&gt;" in text and "AT&T <bad>" not in text
    for sleeve, group in (("A", "shelf"), ("B", "now"), ("X", "now")):
        res = BL.run_once([], sleeve=sleeve, group=group)
        assert res[0]["gate"] == "input", (sleeve, group, res)
    for bad_ed in (True, "bad", {}, float("nan"), float("inf")):
        sig = _sig(code="AAPL", ccy="USD", earnings_d=bad_ed)
        res, ex, _, _ = _run([sig], holdings={})
        assert not ex.called and _g(res, "AAPL")["gate"] == "input", bad_ed
    print("[PASS] signal id/name/earnings + sleeve/group 계약 · HTML escape")


def test_boolean_numbers_rejected_everywhere():
    """Codex V5 P1-3 재현: bool은 int 하위 타입이라 float(True)==1.0 —
    JSON true가 가격·손절·눌림가·목표·환율 1.0으로 주문됐다."""
    cases = (
        {"entry": True, "stop": 0.5},
        {"entry": 2.0, "stop": True},
        {"entry": 2.0, "stop": 0.5, "tactic": {"mode": "pullback",
                                               "pb_price": True}},
        {"entry": 2.0, "stop": 0.5, "target": True},
    )
    for kw in cases:
        sig = _sig(code="AAPL", ccy="USD", **kw)
        res, ex, _, _ = _run([sig], holdings={}, last=1.0)
        assert not ex.called, kw
        assert _g(res, "AAPL")["gate"] in ("input", "tactic"), (kw, res)
    res, ex, _, _ = _run([_sig(code="AAPL", ccy="USD")], holdings={},
                         run_kwargs={"fx": True})
    assert not ex.called and res[0]["gate"] == "input"       # fx=True → 1.0 금지
    print("[PASS] boolean 가격·손절·눌림·목표·환율 전부 주문 0(공용 파서 거부)")


def test_ccy_market_mismatch_blocked():
    """Codex V5 P2-1 재현: AAPL/KRW가 KR 주문 경로로, 005930/USD가 US 경로로
    전달됐다 — 거절 주문·원장 소음·일일 카운트 소모. 통화 정규화 + 허용집합 +
    심볼 시장 일치를 주문 전에 강제한다."""
    for code, ccy in (("AAPL", "KRW"), ("005930", "USD"),
                      ("AAPL", "krw"), ("AAPL", "EUR"), ("AAPL", None)):
        sig = _sig(code=code, ccy=ccy, entry=100.0, stop=95.0)
        res, ex, _, _ = _run([sig], holdings={})
        assert not ex.called, (code, ccy)
        assert _g(res, code)["gate"] == "input", (code, ccy, res)
    # 정상 조합·소문자 정규화는 통과.
    for code, ccy in (("AAPL", "USD"), ("005930", "KRW"), ("005930", "krw")):
        res, ex, _, _ = _run([_sig(code=code, ccy=ccy)], holdings={})
        assert ex.called, (code, ccy)
        assert ex.call_args.kwargs["market"] == ("KR" if code == "005930"
                                                 else "US")
    print("[PASS] 통화/시장 불일치·미지원 통화 주문 0 · 정규화 정상 통과")


def test_execute_entry_rejects_boolean_inputs_directly():
    """실행기 이중방어(Codex V5 P1-3): buyloop 검증과 독립으로 bool 직접 주입
    차단."""
    with mock.patch.dict(os.environ, {"ALLOW_BUY": "1"}):
        d = kis_buy.execute_entry("p#b", "AAPL", price_usd=True,
                                  per_share_risk_usd=5.0, krw_per_usd=1400.0)
        assert d.gate == "input" and "boolean" in d.why, d
        d = kis_buy.execute_entry("p#b", "AAPL", price_usd=100.0,
                                  per_share_risk_usd=True, krw_per_usd=1400.0)
        assert d.gate == "input", d
        d = kis_buy.execute_entry("p#b", "AAPL", price_usd=100.0,
                                  per_share_risk_usd=5.0, krw_per_usd=True)
        assert d.gate == "input", d
        d = kis_buy.execute_entry("p#b", "AAPL", price_usd=100.0,
                                  per_share_risk_usd=5.0, krw_per_usd=1400.0,
                                  order_meta={"stop": True})
        assert d.gate == "input", d
    print("[PASS] 실행기 직접 bool 주입도 input 차단(이중방어)")


def test_property_sweep_only_validated_values_reach_executor():
    """속성 스윕(Codex V5 §5·§7, V6 강화): 타입·경계 조합 × A/B 그룹에서 —
    ① 실행기 도달 인자는 전부 검증된 값(bool 아님·finite·양수, 주문가>stop,
       target은 A: None 또는 >stop / **B: > 실제 발주가**),
    ② 미주문 조합은 이 하네스가 관측하는 부작용(보호 포지션 기록·원장 이벤트)
       0. 주문 primitive·costbook까지의 전체 부작용은 실행기 실체 테스트
       (test_kis_buy_gates.test_run_once_end_to_end_side_effects)가 본다."""
    nan, inf = float("nan"), float("inf")
    entries = (100.0, "100", 0, -1, True, nan, None)
    stops = (95.0, 0, 100.0, 120.0, True, nan)
    tactics = (None, "full", {"mode": "pullback", "pb_price": 97.0},
               {"mode": []}, [], "banana", {"mode": "half", "pb_price": True})
    targets = (None, 110.0, 100.2, 99.0, True, 0)
    combos = [(e, st, ta, tg, grp)
              for e in entries for st in stops for ta in tactics
              for tg in targets for grp in ("now", "shelf")]
    sent = blocked = 0
    for i, (e, st, ta, tg, grp) in enumerate(combos):
        if i % 5:                                  # 결정론적 표본(런타임 상한)
            continue
        extra = {"shelf": {"rr": 2.0}} if grp == "shelf" else {}
        sig = _sig(code="AAPL", ccy="USD", entry=e, stop=st, group=grp, **extra)
        if ta is not None:
            sig["tactic"] = ta
        if tg is not None:
            sig["target"] = tg
        else:
            sig.pop("target", None)                # shelf 기본 주입 제거 — 원값
        sleeve = "B" if grp == "shelf" else "A"
        res, ex, recorded, ledger_events = _run(
            [sig], holdings={}, last=100.4,
            run_kwargs={"sleeve": sleeve, "group": grp})
        if ex.called:
            sent += 1
            kw = ex.call_args.kwargs
            for field in ("price_usd", "per_share_risk_usd", "krw_per_usd"):
                v = kw[field]
                assert not isinstance(v, bool) and math.isfinite(v) and v > 0, \
                    (field, v, e, st, ta, tg, grp)
            meta = kw["order_meta"]
            assert not isinstance(meta["stop"], bool)
            assert math.isfinite(meta["stop"]) and meta["stop"] > 0
            assert kw["price_usd"] > meta["stop"], (e, st, ta, tg, grp)
            if grp == "shelf":
                # V6 P1-1 속성: B target은 실제 발주가보다 커야 한다.
                assert meta["target"] is not None \
                    and meta["target"] > kw["price_usd"], (e, st, ta, tg)
            else:
                assert meta["target"] is None or (
                    not isinstance(meta["target"], bool)
                    and math.isfinite(meta["target"])
                    and meta["target"] > meta["stop"]), (e, st, ta, tg)
        else:
            blocked += 1
        # 미주문 조합의 하네스 관측 부작용 0(실행기 mock 하에서의 보증 범위).
        assert recorded == {}, (e, st, ta, tg, grp)
        if not ex.called:
            assert ledger_events == [], (e, st, ta, tg, grp)
    assert sent > 0 and blocked > sent             # 스윕이 실제로 양쪽을 커버
    print(f"[PASS] 속성 스윕 sent {sent} · blocked {blocked} "
          f"(총 {sent + blocked}조합, A/B 포함)")


def test_intra_cycle_accumulation():
    """한 사이클 두 매수 — 두 번째 호출은 첫 매수를 반영한 캡 입력을 받아야."""
    res, ex, rec, _ = _run([_sig(code="005930"), _sig(code="000660")], holdings={})
    assert len(ex.call_args_list) == 2
    k1 = ex.call_args_list[0].kwargs
    k2 = ex.call_args_list[1].kwargs
    assert k1["open_positions"] == 0 and k1["open_cost_krw"] == 0
    assert k2["open_positions"] == 1                       # 첫 매수 즉시 반영
    assert k2["open_cost_krw"] == 3 * 100.0                # qty3 × last100 (KR fx=1)
    print("[PASS] 사이클 내 누적 — 같은 스냅샷 연속매수로 SEED 초과하는 구멍 차단")


def test_non_now_filtered():
    res, _, rec, _ = _run([_sig(group="watch"), _sig(code="000660", fresh=False)],
                       holdings={})
    assert res == []                                 # 후보 아님
    print("[PASS] now 아님·미신선 → 후보 제외")


def test_us_signal_routes_and_fx():
    ex_ret = kis_buy.BuyDecision(True, "sent", "ack", qty=1)
    res, ex, rec, _ = _run([_sig(code="AAPL", ccy="USD", entry=190.0, stop=185.0)],
                        holdings={}, last=190.5, exec_ret=ex_ret)
    assert _g(res, "AAPL")["ok"]
    assert ex.call_args.kwargs.get("market") == "US"
    assert ex.call_args.kwargs.get("krw_per_usd") > 0   # fx 전달
    print("[PASS] 미국 신호 → market=US·fx 전달")


def test_tactic_half_creates_persistent_second_order():
    d = kis_buy.BuyDecision(True, "sent", "ack", qty=5, planned_qty=10)
    sig = _sig(entry=100.0, stop=93.0,
               tactic={"mode": "half", "pb_price": 97.0})
    res, ex, rec, events = _run([sig], holdings={}, exec_ret=d)
    assert ex.call_args.kwargs["qty_fraction"] == 0.5
    plans = [e for e in events if e.get("ev") == "plan"]
    assert len(plans) == 1 and plans[0]["intended"] == 5
    assert plans[0]["meta"]["limit"] == 97.0 and plans[0]["meta"]["pending"]
    assert rec == {}
    print("[PASS] tactic=half: 1차 절반 + 원장에 2차 눌림 5주 영속")


def test_tactic_pullback_uses_limit_without_chasing():
    d = kis_buy.BuyDecision(True, "sent", "ack", qty=7, planned_qty=7)
    sig = _sig(entry=100.0, stop=85.0,
               tactic={"mode": "pullback", "pb_price": 90.0})
    res, ex, _, _ = _run([sig], holdings={}, last=120.0, exec_ret=d)
    kw = ex.call_args.kwargs
    assert kw["price_usd"] == 90.0 and kw["limit_price"] == 90.0
    assert kw["order_meta"]["pending"] is True
    assert _g(res, "005930")["tactic"] == "pullback"
    print("[PASS] tactic=pullback: 현재가 추격 없이 눌림가 전량 지정가")


def test_b_sleeve_survives_balance_before_position_reconcile():
    """B ACK가 잔고에 먼저 보이면 kis_positions 전에도 B 원가/개수로 남아야."""
    import time as _t
    fold = {"sb:alk": {
        "symbol": "005930", "side": "BUY", "state": "ack",
        "submitted_at": _t.time(), "intended": 8, "filled": 0,
        "hldg_before": 0, "price": 100.0, "market": "KR",
        "sleeve": "B", "pos_key": "sb:alk"}}
    sig = _sig(code="000660", group="shelf")
    _, ex, _, _ = _run(
        [sig], holdings={"005930": 8}, fold=fold,
        run_kwargs={"sleeve": "B", "group": "shelf", "seed_krw": 5_000_000})
    kw = ex.call_args.kwargs
    assert kw["open_positions"] == 1
    assert kw["open_cost_krw"] == 800.0
    assert kw["total_open_cost_krw"] == 800.0
    print("[PASS] B ACK→잔고 선반영→kpos 지연에도 B 귀속·예산 유지")


def test_unfilled_b_plan_does_not_retag_existing_a_holding():
    """미제출 B 계획만으로 기존 A 보유가 B 원가·포지션으로 이동하지 않는다."""
    import time as _t
    fold = {"sb:planned": {
        "symbol": "005930", "side": "BUY", "state": "planned",
        "created_at": _t.time(), "intended": 2, "filled": 0,
        "price": 90.0, "market": "KR", "sleeve": "B",
        "pos_key": "sb:planned"}}
    sig = _sig(code="000660", group="shelf")
    _, ex, _, _ = _run(
        [sig], holdings={"005930": 5}, fold=fold,
        run_kwargs={"sleeve": "B", "group": "shelf", "seed_krw": 5_000_000})
    kw = ex.call_args.kwargs
    assert kw["open_positions"] == 1   # B 계획 자체는 B의 열린 예약 포지션
    assert kw["open_cost_krw"] == 2 * 90
    assert kw["total_open_cost_krw"] == 5 * 100 + 2 * 90
    print("[PASS] 미체결 B 계획은 기존 A 보유를 재태깅하지 않고 예약만 B에 반영")


def test_partial_and_multiple_same_symbol_reservations_are_summed():
    """보유 6 + 1차잔량4 + 눌림계획5를 같은 B 포지션 1개·원가 전부로 계산."""
    import time as _t
    fold = {
        "sb:alk": {
            "symbol": "005930", "side": "BUY", "state": "partial",
            "submitted_at": _t.time(), "intended": 10, "filled": 0,
            "hldg_before": 0, "price": 100.0, "market": "KR",
            "sleeve": "B", "pos_key": "sb:alk", "open": True},
        "sb:alk:pb": {
            "symbol": "005930", "side": "BUY", "state": "planned",
            "created_at": _t.time() + 1, "intended": 5, "filled": 0,
            "price": 90.0, "market": "KR", "sleeve": "B",
            "pos_key": "sb:alk"},
    }
    sig = _sig(code="000660", group="shelf")
    _, ex, _, _ = _run(
        [sig], holdings={"005930": 6}, fold=fold,
        run_kwargs={"sleeve": "B", "group": "shelf", "seed_krw": 5_000_000})
    kw = ex.call_args.kwargs
    assert kw["open_positions"] == 1
    assert kw["open_cost_krw"] == 600 + 4 * 100 + 5 * 90
    assert kw["total_open_cost_krw"] == 1450
    print("[PASS] 동종목 부분잔량·계획 예약 누적, dict 덮어쓰기 제거")


def main():
    test_happy_path_executes()
    test_already_held_skips()
    test_nyse_holding_merged()
    test_holdings_unknown_skips()
    test_price_deviation_skips()
    test_market_closed_skips()
    test_earnings_window_skips()
    test_cooldown_after_same_day_sell()
    test_inflight_buy_counts_toward_position_accounting()
    test_b_sleeve_has_no_fixed_position_count_gate()
    test_a_fresh_signal_executes_without_autopaper()
    test_no_autopaper_network_call_in_buyloop()
    test_a_outside_tolerance_then_inside_executes_once()
    test_malformed_signal_is_fail_closed()
    test_no_order_on_nan_zero_negative_or_inverted_stop()
    test_a_has_no_fixed_position_count_cap()
    test_non_boolean_fresh_is_rejected_for_a_too()
    test_b_stale_row_in_fresh_document_is_rejected()
    test_nan_quote_is_gated_without_crash()
    test_corrupt_target_is_rejected_not_persisted()
    test_no_entry_when_price_at_or_below_stop()
    test_b_requires_valid_target_above_entry()
    test_b_target_must_exceed_actual_order_price()
    test_corrupt_priority_fields_reject_row_not_cycle()
    test_b_actual_rr_and_stop_width_boundaries()
    test_signal_identity_name_and_call_contract()
    test_boolean_numbers_rejected_everywhere()
    test_ccy_market_mismatch_blocked()
    test_execute_entry_rejects_boolean_inputs_directly()
    test_property_sweep_only_validated_values_reach_executor()
    test_invalid_fx_fails_closed_for_all_candidates()
    test_unknown_tactic_never_bypasses_tolerance()
    test_falsy_tactic_values_do_not_default_to_full()
    test_stop_at_or_above_entry_blocked_regardless_of_price()
    test_intra_cycle_accumulation()
    test_non_now_filtered()
    test_us_signal_routes_and_fx()
    test_tactic_half_creates_persistent_second_order()
    test_tactic_pullback_uses_limit_without_chasing()
    test_b_sleeve_survives_balance_before_position_reconcile()
    test_unfilled_b_plan_does_not_retag_existing_a_holding()
    test_partial_and_multiple_same_symbol_reservations_are_summed()
    print("\n매수 루프 검증 통과 — 스캐너 직접집행(고정 종목수 무제한·예산 누적).")


if __name__ == "__main__":
    main()
