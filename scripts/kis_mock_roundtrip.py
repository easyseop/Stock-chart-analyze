#!/usr/bin/env python3
"""KIS 모의 주문 왕복 검증 (Stage 1.5) — 실계좌 아님·모의 전용·기본 안 체결 흐름.

무엇을 하나(기본 모드 — 포지션을 만들지 않음):
  1. 현재가 조회 → **체결 안 될 낮은 지정가**(현재가의 50%)로 매수 1주 접수
  2. ODNO 수신·원장 결속 확인
  3. nccs(미체결)에 그 주문이 보이는지 확인   ← 대사 채널 A 실증
  4. 그 주문 **취소** → 취소 접수 확인
  5. ccnl(체결내역)에 주문/취소 흔적 확인      ← 대사 채널 B 실증
  → 돈 0원·포지션 0에서 주문 API 전 경로(생성→조회→취소)를 왕복 검증.

--fill 모드(선택): 마켓터블 지정가로 매수 1주 실제 체결 → ccnl 확인 → 즉시
  마켓터블 매도 1주(포지션 청산)까지. 모의 체결 시뮬레이션 품질 관찰용.

사용(집 컴퓨터, 모의 키 환경변수 세팅돼 있어야 함 — kis_probe.py와 동일):
  export KIS_ENV=mock KIS_MOCK_APPKEY=... KIS_MOCK_APPSECRET=... KIS_MOCK_CANO=...
  export KIS_ORDERS_ENABLED=1        # ← 주문 게이트(명시해야 전송됨)
  python scripts/kis_mock_roundtrip.py                # 기본: 안 체결 왕복
  python scripts/kis_mock_roundtrip.py --symbol AAPL --excg NASD
  python scripts/kis_mock_roundtrip.py --fill         # 체결까지(모의 관찰용)

안전:
  · live 하드블록 — KIS_ENV=live면 kis_orders가 전송 자체를 거부한다.
  · 모의 미국주문은 지정가(00)만·정규장 시간대에만 접수될 수 있음(KST 밤).
    장 아니면 접수 거부가 정상 — 그 메시지도 수확(msg_cd 기록).
  · 모의 2건/초 유량 → 각 단계 사이 0.7s 대기.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import kis, kis_orders, ledger  # noqa: E402


def say(x: str) -> None:
    print(x, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="KIS 모의 주문 왕복(기본: 안 체결)")
    ap.add_argument("--symbol", default="AAPL")
    ap.add_argument("--excg", default="NASD")
    ap.add_argument("--fill", action="store_true",
                    help="마켓터블로 실제 체결(매수→매도 청산)까지 관찰")
    args = ap.parse_args()

    say(f"env={kis.ENV} base={kis.BASE_URL}")
    ok, why = kis_orders.orders_allowed()
    if not ok:
        say(f"✗ 주문 게이트: {why}")
        say("  (모의 키 + KIS_ORDERS_ENABLED=1 필요. live는 어떤 경우에도 거부됨)")
        return 1

    # 0) 현재가 — 시세로 주문가 계산 (모의도 현재체결가는 지원)
    #    시세 TR(HHDFS00000300)은 실전/모의 동일(V 접두 규칙 밖) [대조필요]
    d = kis._get("/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                 {"AUTH": "", "EXCD": {"NASD": "NAS", "NYSE": "NYS",
                                       "AMEX": "AMS"}.get(args.excg, "NAS"),
                  "SYMB": args.symbol})
    last = 0.0
    try:
        last = float(((d or {}).get("output") or {}).get("last") or 0)
    except (TypeError, ValueError):
        pass
    if last <= 0:
        say("✗ 현재가 조회 실패 — 장/심볼/유량 확인 (출력 공유해줘)")
        say(f"   raw={json.dumps((d or {}), ensure_ascii=False)[:300]}")
        return 1
    say(f"✓ 현재가 {args.symbol} = {last}")
    time.sleep(0.8)

    pos = f"rt:{args.symbol}:{int(time.time())}"
    if not args.fill:
        # 1) 체결 안 될 낮은 지정가 매수 1주
        px = round(last * 0.5, 2)
        say(f"\n[1] 매수 1주 @ {px} (현재가의 50% — 체결 안 됨) 접수")
        r = kis_orders.place_buy(f"{pos}#1", args.symbol, 1, px,
                                 excg=args.excg, reason="왕복검증",
                                 min_interval_s=0.0)
        say(f"    → {r}")
        if not r.get("ok"):
            say("    접수 실패 — 장시간 밖이면 정상(메시지 수확). 출력 공유해줘.")
            return 0
        odno = r["odno"]
        time.sleep(0.8)

        # 2) nccs에서 보이나 (대사 채널 A)
        say("\n[2] 미체결(nccs) 확인")
        d = kis.open_orders(excg=args.excg)
        rows = (d or {}).get("output") or []
        mine = [x for x in rows if str(x.get("odno")) == str(odno)]
        say(f"    미체결 {len(rows)}건 중 내 주문(ODNO={odno}): "
            f"{'✓ 발견' if mine else '✗ 없음'}")
        time.sleep(0.8)

        # 3) 취소
        say("\n[3] 취소 접수")
        r2 = kis_orders.cancel_order(f"{pos}#1:cxl", args.symbol, odno, 1,
                                     excg=args.excg)
        say(f"    → {r2}")
        time.sleep(0.8)

        # 4) ccnl 흔적 (대사 채널 B)
        say("\n[4] 체결내역(ccnl) 확인")
        d = kis.fills(excg=args.excg)
        rows = (d or {}).get("output") or []
        mine = [x for x in rows if str(x.get("odno")) == str(odno)]
        say(f"    최근 내역 {len(rows)}건 중 ODNO={odno}: "
            f"{'✓ 발견' if mine else '(미표시 — 취소건 표시는 실측 포인트)'}")
        if mine:
            say("    " + json.dumps(mine[0], ensure_ascii=False)[:300])
    else:
        # --fill: 마켓터블 매수 → ccnl → 마켓터블 매도(청산)
        bpx = kis_orders.marketable_limit_price(last, "BUY")
        say(f"\n[1] 마켓터블 매수 1주 @ {bpx}")
        r = kis_orders.place_buy(f"{pos}#1", args.symbol, 1, bpx,
                                 excg=args.excg, reason="체결관찰",
                                 min_interval_s=0.0)
        say(f"    → {r}")
        if not r.get("ok"):
            return 0
        say("    (체결 대기 5s)")
        time.sleep(5)
        d = kis.fills(excg=args.excg)
        rows = (d or {}).get("output") or []
        mine = [x for x in rows if str(x.get("odno")) == str(r["odno"])]
        say(f"[2] ccnl: {'✓ 체결 확인' if mine else '미확인(지연?)'} "
            + (json.dumps(mine[0], ensure_ascii=False)[:250] if mine else ""))
        time.sleep(0.8)
        spx = kis_orders.marketable_limit_price(last, "SELL")
        say(f"\n[3] 마켓터블 매도 1주 @ {spx} (청산)")
        r2 = kis_orders.place_sell(f"{pos}#2", args.symbol, 1, spx,
                                   excg=args.excg, reason="청산",
                                   min_interval_s=0.0)
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
