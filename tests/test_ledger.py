"""주문 원장 상태기계 검증 — 초과매도 방지의 핵심 경로.

  1) 정상: submit→filled → 잠금 없음, 잔여 0
  2) 타임아웃: submit→unknown → 종목 **잠금**(재주문 금지)
  3) 대사: unknown→reconcile(부분체결) → 잠금 해제 + 잔여 = intended−실체결
  4) 잔여만 재주문: 원수량 아닌 잔여 수량으로만
  5) 거부/부분체결 상태 전이
  6) 잠금은 UNKNOWN만 — filled/partial은 잠그지 않음
  7) append-only 재생성 — 재시작(재로드) 후에도 상태 유지

실행: python -m tests.test_ledger
"""
from __future__ import annotations

import os
import multiprocessing
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot.ledger as L
from bot import costbook as C


def _fresh(tmp):
    L.LEDGER_PATH = os.path.join(tmp, "order_ledger.jsonl")


def _race_submit(path, ready, start, results):
    import bot.ledger as child_ledger
    child_ledger.LEDGER_PATH = path
    ready.put(True)
    start.wait(5)
    ok = child_ledger.try_record_submit(
        f"race:{os.getpid()}", "AAPL", 1, min_interval_s=0)
    results.put(ok)


def main() -> int:
    fails = []

    # 1) 정상 체결
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k1", "AAPL", 10, "하드 손절")
        L.on_result("k1", "filled", 10)
        s = L.state_of("k1")
        if s["state"] != "filled" or L.residual_qty("k1", 10) != 0 \
                or L.is_locked("AAPL"):
            fails.append(f"정상 체결 상태 오류: {s} locked={L.is_locked('AAPL')}")
        else:
            print("  [PASS] submit→filled: 잔여0·잠금없음")

    # 2) 타임아웃 → UNKNOWN → 잠금
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k2", "TSLA", 8, "손절")
        L.on_result("k2", "unknown", 0)          # 응답 타임아웃
        if not L.is_locked("TSLA"):
            fails.append("UNKNOWN인데 종목이 안 잠김(초과매도 위험)")
        elif "TSLA" not in L.locked_symbols():
            fails.append("locked_symbols에 TSLA 누락")
        else:
            print("  [PASS] submit→unknown: 종목 잠금(재주문 차단)")

        # 3) 대사: 실제로는 3주만 체결돼 있었음 → 부분체결 확정 + 잠금 해제
        r = L.reconcile("k2", actual_filled=3)
        if r["state"] != "partial" or r["residual"] != 5 or L.is_locked("TSLA"):
            fails.append(f"대사 오류: {r} locked={L.is_locked('TSLA')}")
        else:
            print("  [PASS] unknown→reconcile(부분3): 잠금해제·잔여5")

        # 4) 잔여만 재주문 — 원수량(8) 아닌 잔여(5)
        resid = L.residual_qty("k2", 8)
        if resid != 5:
            fails.append(f"잔여 계산 오류: {resid}(기대 5)")
        else:
            L.record_submit("k2:r2", "TSLA", resid, "잔여 재주문")
            L.on_result("k2:r2", "filled", 5)
            if L.residual_qty("k2:r2", 5) != 0:
                fails.append("잔여 재주문 후 미완결")
            else:
                print("  [PASS] 잔여 수량(5)만 재주문 → 완결")

    # 5) 거부 — 종료 상태, 잠금 없음
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k5", "NVDA", 4)
        L.on_result("k5", "rejected", 0)
        if L.state_of("k5")["state"] != "rejected" or L.is_locked("NVDA"):
            fails.append("거부 상태/잠금 오류")
        else:
            print("  [PASS] rejected: 종료·잠금없음")

    # 6) partial은 잠그지 않음(알려진 부분체결은 잔여 즉시 재주문 가능)
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k6", "META", 10)
        L.on_result("k6", "partial", 6)
        if L.is_locked("META"):
            fails.append("partial인데 잠김(불필요한 재주문 차단)")
        elif L.residual_qty("k6", 10) != 4:
            fails.append(f"partial 잔여 오류: {L.residual_qty('k6', 10)}")
        else:
            print("  [PASS] partial: 미잠금·잔여4")

    # 7) append-only 재생성 — 파일만 있으면 재시작 후에도 상태 복원
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k7", "AMD", 5)
        L.on_result("k7", "unknown", 0)
        # '재시작' 시뮬레이션 — 모듈 상태 없이 파일만으로 재생성
        if not L.is_locked("AMD"):
            fails.append("재생성 후 UNKNOWN 잠금 소실")
        else:
            print("  [PASS] append-only 재생성 — 재시작 내성")

    # 8) KIS 확장 — ODNO 결속
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k8", "AAPL", 3, "손절")
        L.bind_broker_order("k8", odno="0001569157", orgno="06010",
                            ord_tmd="142233")
        if L.odno_of("k8") != "0001569157":
            fails.append(f"ODNO 결속 실패: {L.odno_of('k8')}")
        else:
            print("  [PASS] ODNO 결속 — 정정/취소 핸들 보존")

    # 9) 합성키 — 결정적·반올림
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        a = L.synthetic_key("50000058", "NASD", "AAPL", "SELL", 1, 99.504, "142233")
        b = L.synthetic_key("50000058", "NASD", "AAPL", "SELL", 1, 99.501, "142255")
        c = L.synthetic_key("50000058", "NASD", "AAPL", "SELL", 2, 99.50, "142233")
        if a != b:
            fails.append("합성키: 같은 분·반올림가인데 다름")
        elif a == c:
            fails.append("합성키: 수량 다른데 같음")
        else:
            print("  [PASS] synthetic_key 결정적(분버킷·반올림)·수량 구분")

    # 10) confidence 대사 — 후보 1개=HIGH(해제), 2개=LOW(잠금 유지)
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k10", "TSLA", 10, "손절")
        L.on_result("k10", "unknown", 0)
        r = L.reconcile_from_candidates("k10", [{"filled": 4}], intended=10)
        if r["confidence"] != L.CONF_HIGH or r["residual"] != 6 or L.is_locked("TSLA"):
            fails.append(f"confidence HIGH 대사 오류: {r} locked={L.is_locked('TSLA')}")
        else:
            print("  [PASS] 후보1개=HIGH: 확정·잠금해제·잔여6")

    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("k10b", "TSLA", 10, "손절")
        L.on_result("k10b", "unknown", 0)
        r = L.reconcile_from_candidates("k10b", [{"filled": 4}, {"filled": 5}],
                                        intended=10)
        if r["confidence"] != L.CONF_LOW or not L.is_locked("TSLA"):
            fails.append(f"confidence LOW인데 잠금 해제됨(위험): {r}")
        else:
            print("  [PASS] 후보2개=LOW: 자동해소 금지·잠금 유지")

    # 11) 동일종목 규칙 — in-flight/잠금/최소간격
    #     record_submit이 실제 time.time()으로 stamp하므로, 그 stamp를 기준시각으로 삼는다.
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        ok_first = L.can_submit("NVDA", min_interval_s=60)      # 이력 없음 → 허용
        L.record_submit("k11", "NVDA", 5, "손절")
        sub = L.last_submit_ts("NVDA")                          # 실제 stamp된 제출시각
        # in-flight(submitted) 상태 — 동시 재주문 금지
        blocked_inflight = not L.can_submit("NVDA", min_interval_s=60, now=sub + 1)
        L.on_result("k11", "filled", 5)          # 종료 → in-flight 해제
        # 종료됐지만 60초 이내 — 최소간격으로 여전히 금지
        blocked_interval = not L.can_submit("NVDA", min_interval_s=60, now=sub + 10)
        # 60초 경과 — 허용
        allowed_after = L.can_submit("NVDA", min_interval_s=60, now=sub + 61)
        if not (ok_first and blocked_inflight and blocked_interval and allowed_after):
            fails.append(f"can_submit 규칙 오류: first={ok_first} "
                         f"inflight={blocked_inflight} interval={blocked_interval} "
                         f"after={allowed_after}")
        else:
            print("  [PASS] can_submit: 동시금지·60초간격·경과후허용")

    # 12) 방향별 경합 + 주문 메타의 임시 손절선 복구
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("kb:alk", "ALK", 8, "신규매수", meta={
            "side": "BUY", "market": "US", "stop": 88.5,
            "ccy": "USD", "pos_key": "kb:alk", "opened": "2026-07-25"})
        L.bind_broker_order("kb:alk", "BUY1")
        L.on_result("kb:alk", "ack", 0, open_order=True)
        buy_open = L.open_order_count("ALK", side="BUY")
        sell_open = L.open_order_count("ALK", side="SELL")
        protection = L.provisional_buy_protection("ALK")
        if buy_open != 1 or sell_open != 0:
            fails.append(f"방향별 open count 오류: BUY={buy_open} SELL={sell_open}")
        elif not protection or protection.get("ambiguous") \
                or protection.get("stop") != 88.5:
            fails.append(f"BUY 임시 손절 복구 오류: {protection}")
        else:
            print("  [PASS] BUY/SELL 경합 분리 + ACK 대사 중 임시 손절선 복구")

    # 13) append는 파일 내용과 새 파일 이름을 fsync해 전원손실 창을 닫는다.
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        real_fsync = os.fsync
        with mock.patch.object(L.os, "fsync", wraps=real_fsync) as sync:
            L.record_submit("durable", "AAPL", 1)
        if sync.call_count < 2:
            fails.append(f"fsync 누락: {sync.call_count}회(파일+디렉터리 기대)")
        else:
            print("  [PASS] append 후 파일+디렉터리 fsync")

    # 14) JSONL 손상은 건너뛰고 거래를 계속하지 않고 전면 fail-closed한다.
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        L.record_submit("good", "AAPL", 1)
        with open(L.LEDGER_PATH, "ab") as fp:
            fp.write(b'{"ev":"submit","key":"torn"')
        status = L.corruption_status()
        if status["healthy"] or L.can_submit("MSFT") or "*" not in L.locked_symbols():
            fails.append(f"손상 원장 fail-closed 실패: {status}")
        else:
            print(f"  [PASS] 원장 손상 줄 {status['lines']} → 신규 주문 전면 차단")

    # 15) 두 프로세스의 검사+기록 경합에서도 하나만 submit을 확보한다.
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        ctx = multiprocessing.get_context("fork")
        ready, results = ctx.Queue(), ctx.Queue()
        start = ctx.Event()
        ps = [ctx.Process(target=_race_submit,
                          args=(L.LEDGER_PATH, ready, start, results))
              for _ in range(2)]
        for p in ps:
            p.start()
        ready.get(timeout=5); ready.get(timeout=5)
        start.set()
        got = [results.get(timeout=5), results.get(timeout=5)]
        for p in ps:
            p.join(5)
        if sorted(got) != [False, True]:
            fails.append(f"프로세스 원자 게이트 실패: {got}")
        else:
            print("  [PASS] 프로세스 동시 경합 → submit 정확히 1건")

    # 16) 오래된 submitted는 UNKNOWN 잠금으로 승격되어 대사만 허용한다.
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        t0 = time.time() - 200
        L._append({"ev": "submit", "key": "stale", "symbol": "TSLA",
                   "intended": 2, "filled": 0, "state": "submitted",
                   "meta": {"side": "BUY", "hldg_before": 0}, "ts": t0})
        promoted = L.promote_stale_submitted(90, now=t0 + 100)
        if len(promoted) != 1 or L.state_of("stale")["state"] != "unknown" \
                or not L.is_locked("TSLA"):
            fails.append(f"stale submitted 승격 실패: {promoted}")
        else:
            print("  [PASS] stale submitted → UNKNOWN·종목 잠금")

    # 17) 서로 다른 종목/프로세스라도 A+B 예산 예약은 같은 원장 잠금에서 합산.
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        with mock.patch.dict(
                os.environ,
                {"COSTBOOK_PATH": os.path.join(tmp, "costbook.jsonl")}):
            common = {
                "side": "BUY", "market": "KR", "sleeve": "A", "price": 100,
                "fx": 1, "reservation_cost_krw": 600,
                "budget_total_held_krw": 400,
                "budget_total_limit_krw": 1000,
                "budget_sleeve_held_krw": 400,
                "budget_sleeve_limit_krw": 1000,
            }
            first = L.try_record_submit(
                "budget:1", "AAA", 6, meta=common, min_interval_s=0)
            second = L.try_record_submit(
                "budget:2", "BBB", 1,
                meta={**common, "reservation_cost_krw": 1},
                min_interval_s=0)
        if not first or second:
            fails.append(f"원자 총예산 예약 실패: first={first} second={second}")
        else:
            print("  [PASS] 다른 종목 동시 후보도 A+B 총예산 원자 예약으로 초과 차단")

    # 18) filled→costbook/accounted 전환창에도 예약 유지 + 오래된 held 스냅샷 보정.
    with tempfile.TemporaryDirectory() as tmp:
        _fresh(tmp)
        with mock.patch.dict(
                os.environ,
                {"COSTBOOK_PATH": os.path.join(tmp, "costbook.jsonl")}):
            meta = {
                "side": "BUY", "market": "KR", "sleeve": "A", "price": 100,
                "fx": 1, "reservation_cost_krw": 1000,
            }
            L.record_submit("handoff:old", "AAA", 10, meta=meta)
            L.on_result("handoff:old", "filled", 10, open_order=False)
            stale = {
                **meta, "reservation_cost_krw": 1,
                "budget_total_held_krw": 0,
                "budget_total_limit_krw": 1000,
                "budget_sleeve_held_krw": 0,
                "budget_sleeve_limit_krw": 1000,
            }
            blocked_before = not L.try_record_submit(
                "handoff:before", "BBB", 1, meta=stale, min_interval_s=0)
            C.add_lot("pos:old", "AAA", 10, 100, fx=1, sleeve="A")
            L.mark_accounted("handoff:old", 10)
            # 호출부가 여전히 0원이라는 오래된 브로커 스냅샷을 넘겨도 flock 안의
            # durable costbook 1000원을 읽어 초과 주문을 막아야 한다.
            blocked_after = not L.try_record_submit(
                "handoff:after", "CCC", 1, meta=stale, min_interval_s=0)
        if not (blocked_before and blocked_after):
            fails.append(
                "filled→accounted 총시드 전환창/오래된 스냅샷 차단 실패: "
                f"before={blocked_before} after={blocked_after}")
        else:
            print("  [PASS] filled→durable costbook 전환창·stale held에도 총시드 유지")

    print()
    if fails:
        print("❌ 실패:")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 주문 원장 전부 통과 — UNKNOWN 잠금·대사·잔여만 재주문 + KIS 확장"
          "(ODNO·합성키·confidence·동일종목 규칙).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
