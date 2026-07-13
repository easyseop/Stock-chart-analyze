#!/usr/bin/env python3
"""KIS 모의 주문 왕복 검증 (Stage 1.5) — 실계좌 아님·모의 전용·기본 안 체결 흐름.

국내(KR)·미국(US) 둘 다 지원 — 심볼로 자동 판별(6자리 숫자=국내), --market로 강제 가능.

무엇을 하나(기본 모드 — 포지션을 만들지 않음):
  1. 현재가 조회 → **체결 안 될 낮은 지정가**(현재가의 50%)로 매수 1주 접수
  2. ODNO(+국내는 ORGNO) 수신·원장 결속 확인
  3. nccs(미체결)에 그 주문이 보이는지 확인   ← 대사 채널 A 실증
  4. 그 주문 **취소** → 취소 접수 확인
  5. ccnl(체결내역)에 주문/취소 흔적 확인      ← 대사 채널 B 실증
  → 돈 0원·포지션 0에서 주문 API 전 경로(생성→조회→취소)를 왕복 검증.

--fill 모드(선택): 마켓터블 지정가로 매수 1주 실제 체결 → ccnl 확인 → 즉시
  마켓터블 매도 1주(포지션 청산)까지. 모의 체결 시뮬레이션 품질 관찰용.

사용(집 컴퓨터, 모의 키 환경변수 세팅돼 있어야 함 — kis_probe.py와 동일):
  export KIS_ENV=mock KIS_MOCK_APPKEY=... KIS_MOCK_APPSECRET=... KIS_MOCK_CANO=...
  export KIS_ORDERS_ENABLED=1        # ← 주문 게이트(명시해야 전송됨)
  python scripts/kis_mock_roundtrip.py                       # 미국 기본(AAPL)
  python scripts/kis_mock_roundtrip.py --symbol 005930       # 국내(삼성전자, 자동판별)
  python scripts/kis_mock_roundtrip.py --symbol AAPL --excg NASD
  python scripts/kis_mock_roundtrip.py --symbol 005930 --fill # 체결까지(모의 관찰용)

안전:
  · live 하드블록 — KIS_ENV=live면 kis_orders가 전송 자체를 거부한다.
  · 모의 주문은 지정가(00)만·해당 시장 정규장 시간대에만 접수될 수 있음
    (국내=낮 09:00~15:30 KST / 미국=밤 22:30~05:00 KST).
    장 아니면 접수 거부가 정상 — 그 메시지도 수확(msg_cd 기록).
  · 모의 2건/초 유량 → 각 단계 사이 0.7s 대기.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kis, kis_orders, ledger  # noqa: E402


def say(x: str) -> None:
    print(x, flush=True)


def _held_qty(symbol: str, market: str, excg: str):
    """잔고 보유수량(대사 채널). 조회 실패=None, 미보유=0, 보유=수량.
    국내 VTTC8434R / 해외 잔고 — output1 행에서 종목 매칭. 필드명 [대조필요]."""
    if market == "KR":
        d = kis.domestic_balance()
        pdno_keys, qty_keys = ("pdno",), ("hldg_qty", "ord_psbl_qty")
    else:
        d = kis.overseas_balance(excg=excg)
        pdno_keys = ("ovrs_pdno", "pdno")
        qty_keys = ("ovrs_cblc_qty", "ord_psbl_qty")
    if not d:
        return None
    for row in (d.get("output1") or []):
        code = next((str(row.get(k)) for k in pdno_keys if row.get(k)), "")
        if code == str(symbol):
            for k in qty_keys:
                v = row.get(k)
                if v not in (None, ""):
                    try:
                        return int(float(v))
                    except (TypeError, ValueError):
                        pass
            return 0
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="KIS 모의 주문 왕복(기본: 안 체결)")
    ap.add_argument("--symbol", default="AAPL")
    ap.add_argument("--excg", default="NASD", help="미국만 사용(NASD/NYSE/AMEX)")
    ap.add_argument("--market", choices=["KR", "US"], default=None,
                    help="미지정 시 심볼로 자동판별(6자리 숫자=KR)")
    ap.add_argument("--fill", action="store_true",
                    help="마켓터블로 실제 체결(매수→매도 청산)까지 관찰")
    ap.add_argument("--ledger", default="",
                    help="원장 경로(기본: 실행마다 새 임시 원장으로 격리)")
    args = ap.parse_args()

    # 검증 스크립트는 **실행마다 깨끗한 임시 원장**을 쓴다. 실제 봇 원장을 오염시키지
    #   않고, 직전 왕복의 ack(in-flight) 찌꺼기가 다음 실행을 막지 않게(동일종목 1건
    #   게이트). 단독 실행이라 대사(reconcile)가 원주문을 안 닫아 찌꺼기가 남기 때문.
    ledger.LEDGER_PATH = args.ledger or os.path.join(
        tempfile.gettempdir(), "kis_roundtrip_ledger.jsonl")
    if not args.ledger:
        try:
            os.remove(ledger.LEDGER_PATH)
        except OSError:
            pass
    say(f"[원장 격리] {ledger.LEDGER_PATH}")
    market = args.market or kis.market_of_symbol(args.symbol)
    unit = "원" if market == "KR" else "$"

    say(f"env={kis.ENV} base={kis.BASE_URL} market={market} symbol={args.symbol}")
    ok, why = kis_orders.orders_allowed()
    if not ok:
        say(f"✗ 주문 게이트: {why}")
        say("  (모의 키 + KIS_ORDERS_ENABLED=1 필요. live는 어떤 경우에도 거부됨)")
        return 1

    # 0) 현재가 — 시세로 주문가 계산(국내/해외 자동 라우팅)
    last = kis.last_price(args.symbol, market=market, excg=args.excg)
    if not last or last <= 0:
        say("✗ 현재가 조회 실패 — 장/심볼/유량 확인 (출력 공유해줘)")
        return 1
    say(f"✓ 현재가 {args.symbol} = {last}{unit}")
    time.sleep(0.8)

    pos = f"rt:{args.symbol}:{int(time.time())}"
    if not args.fill:
        # 1) 체결 안 될 낮은 지정가 매수 1주.
        #    국내: 현재가×50%는 일일 하한가(±30%) 아래라 거부(실측 40270000) →
        #      하한가에 매수(항상 유효·매수 미체결). 미국: 현재가×50%(무제한 밴드).
        if market == "KR":
            bands = kis.price_limits(args.symbol)
            if bands:
                px = float(bands[0])              # 하한가
                note = "하한가 — 체결 안 됨"
            else:
                px = kis_orders.marketable_limit_price(
                    last * 0.72, "SELL", 0, market="KR")  # -28%(밴드 내 폴백)
                note = "≈-28% — 체결 안 됨(하한가 조회 실패 폴백)"
        else:
            px = round(last * 0.5, 2)
            note = "현재가의 50% — 체결 안 됨"
        say(f"\n[1] 매수 1주 @ {px}{unit} ({note}) 접수")
        r = kis_orders.place_buy(f"{pos}#1", args.symbol, 1, px,
                                 excg=args.excg, market=market,
                                 reason="왕복검증", min_interval_s=0.0)
        say(f"    → {r}")
        if not r.get("ok"):
            say("    접수 실패 — 장시간 밖이면 정상(메시지 수확). 출력 공유해줘.")
            return 0
        odno = r["odno"]
        orgno = r.get("orgno", "")
        time.sleep(0.8)

        # 2) nccs에서 보이나 (대사 채널 A)
        say("\n[2] 미체결(nccs) 확인")
        d = kis.domestic_open_orders() if market == "KR" \
            else kis.open_orders(excg=args.excg)
        rows = (d or {}).get("output") or []
        mine = [x for x in rows if str(x.get("odno")) == str(odno)]
        say(f"    미체결 {len(rows)}건 중 내 주문(ODNO={odno}): "
            f"{'✓ 발견' if mine else '✗ 없음'}")
        time.sleep(0.8)

        # 3) 취소(국내는 orgno 필요)
        say("\n[3] 취소 접수")
        r2 = kis_orders.cancel_order(f"{pos}#1:cxl", args.symbol, odno, 1,
                                     excg=args.excg, orgno=orgno, market=market)
        say(f"    → {r2}")
        time.sleep(0.8)

        # 4) ccnl 흔적 (대사 채널 B)
        say("\n[4] 체결내역(ccnl) 확인")
        d = kis.domestic_fills() if market == "KR" else kis.fills(excg=args.excg)
        rows = (d or {}).get("output1") or (d or {}).get("output") or []
        mine = [x for x in rows if str(x.get("odno")) == str(odno)]
        say(f"    최근 내역 {len(rows)}건 중 ODNO={odno}: "
            f"{'✓ 발견' if mine else '(미표시 — 취소건 표시는 실측 포인트)'}")
        if mine:
            say("    " + json.dumps(mine[0], ensure_ascii=False)[:300])
    else:
        # --fill: 마켓터블 매수 → **잔고로 체결 확인**(국내 nccs 모의 미지원 → 대사
        #   채널은 잔고) → 원장에 체결 반영 → 청산. 매수가 in-flight로 남으면 동일종목
        #   게이트가 매도를 막으므로, 잔고로 확인된 체결을 원장에 반영해야 청산 가능.
        buy_key = f"{pos}#1"
        bpx = kis_orders.marketable_limit_price(last, "BUY", market=market)
        say(f"\n[1] 마켓터블 매수 1주 @ {bpx}{unit}")
        r = kis_orders.place_buy(buy_key, args.symbol, 1, bpx,
                                 excg=args.excg, market=market,
                                 reason="체결관찰", min_interval_s=0.0)
        say(f"    → {r}")
        if not r.get("ok"):
            return 0
        odno, orgno = r["odno"], r.get("orgno", "")
        say("    (체결 대기 5s)")
        time.sleep(5)

        # [2] 잔고 확인 — 대사 채널(국내=VTTC8434R). 체결 판정의 source of truth.
        held = _held_qty(args.symbol, market, args.excg)
        say(f"[2] 잔고 보유수량({args.symbol}): "
            f"{held if held is not None else '조회 실패'}")

        # [3] ccnl 행 구조 관찰(대사 채널 B 필드명 확보용)
        d = kis.domestic_fills() if market == "KR" else kis.fills(excg=args.excg)
        rows = (d or {}).get("output1") or (d or {}).get("output") or []
        mine = [x for x in rows if str(x.get("odno")) == str(odno)]
        say(f"[3] ccnl {len(rows)}건 중 ODNO={odno}: "
            f"{'✓ 발견' if mine else '미표시'}")
        if mine:
            say("    " + json.dumps(mine[0], ensure_ascii=False)[:400])

        # [4] 잔고로 체결 확인되면 원장 반영 후 청산. 아니면 매수 취소로 정리.
        if held and held >= 1:
            ledger.on_result(buy_key, "filled", int(held))   # 관측 기반 대사
            spx = kis_orders.marketable_limit_price(last, "SELL", market=market)
            say(f"\n[4] 마켓터블 매도 1주 @ {spx}{unit} (청산)")
            r2 = kis_orders.place_sell(f"{pos}#2", args.symbol, 1, spx,
                                       excg=args.excg, market=market,
                                       reason="청산", min_interval_s=0.0)
            say(f"    → {r2}")
        else:
            say("\n[4] 잔고 미반영(모의 미체결/지연) — 매수 취소로 정리")
            r2 = kis_orders.cancel_order(buy_key + ":cxl", args.symbol, odno, 1,
                                         excg=args.excg, orgno=orgno, market=market)
            say(f"    → {r2}")

    say("\n[원장] " + json.dumps(
        {k: {kk: v.get(kk) for kk in ("state", "odno", "intended", "filled")}
         for k, v in ledger._fold().items() if k.startswith(pos)},
        ensure_ascii=False))
    say("\n완료 — 출력 전체(시크릿 없음)를 공유해줘. 특히: 접수 msg_cd,"
        " nccs 표시 여부, 취소 후 ccnl 표기 형태(실측 포인트).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
