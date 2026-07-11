#!/usr/bin/env python3
"""I5 — 장전 preflight 캐너리(읽기 전용·주문 없음) — 지금(주말·장외)도 실행 가능.

장 시작 전(또는 아무 때나) 시스템이 매매 가능한 상태인지 점검한다:
  [읽기 preflight]  토큰 · 해외잔고 · 미체결(nccs) · 체결내역(ccnl)
  [게이트 상태]     kill-switch 레벨 · 부팅 대사 · heartbeat SLA ·
                    baseline(IS2) 캡처 여부 · SEED/플래그 환경변수 새니티
  [환경 새니티]     env(mock/live) 일치 · live인데 ALLOW_BUY/ORDERS 켜짐 경고

주문 권한 자체는 주문 없이 완전 검증 불가(Codex I5) — 그건 장중 왕복(Stage 1.5)
스크립트가 담당. 이 캐너리는 "그 전에 이미 깨져 있는 것"을 아침에 잡는 용도.

사용:  (kis_probe.py와 동일한 KIS_* 환경변수 세팅 후)
  python scripts/kis_preflight.py
종료코드: 0=green · 1=하나라도 FAIL(신규 진입 금지 권고).
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import heartbeat, kill, kis, kis_boot, ownership  # noqa: E402


def _mark(ok: bool | None) -> str:
    return {True: "✓", False: "✗", None: "―"}[ok]


def main() -> int:
    fails = 0
    warns = 0
    print(f"env={kis.ENV} base={kis.BASE_URL}\n")

    # ── 읽기 preflight ────────────────────────────────────────────
    tok_ok = kis._token() is not None
    print(f"[{_mark(tok_ok)}] 토큰 발급/캐시")
    fails += (not tok_ok)

    checks = []
    if kis.account():
        time.sleep(0.7)
        bal = kis.overseas_balance()
        checks.append(("해외잔고", bal is not None and bal.get("rt_cd") == "0"))
        time.sleep(0.7)
        nccs = kis.open_orders()
        checks.append(("미체결(nccs)", nccs is not None and nccs.get("rt_cd") == "0"))
        time.sleep(0.7)
        ccnl = kis.fills()
        checks.append(("체결내역(ccnl)", ccnl is not None and ccnl.get("rt_cd") == "0"))
    else:
        checks.append(("계좌(CANO)", False))
    for name, ok in checks:
        print(f"[{_mark(ok)}] {name}")
        fails += (not ok)

    # ── 게이트 상태 ───────────────────────────────────────────────
    lv = kill.level()
    print(f"[{_mark(lv == 0)}] kill-switch L{lv}" + ("" if lv == 0 else " — 신규 제한 중"))
    warns += (lv > 0)

    unknowns = len(kis_boot.pending_unknowns())
    print(f"[{_mark(unknowns == 0)}] 미해소 UNKNOWN {unknowns}건"
          + ("" if unknowns == 0 else " — 부팅 대사 필요"))
    warns += (unknowns > 0)

    age = heartbeat.age_s()
    sla = heartbeat.sla_status(age, has_positions=False)
    print(f"[{_mark(sla == heartbeat.OK)}] 파수꾼 heartbeat "
          f"(age={'-' if age is None else f'{age:.0f}s'}, {sla})"
          + ("" if sla == heartbeat.OK else " — 파수꾼 미가동?"))
    warns += (sla != heartbeat.OK)

    base = ownership.baseline()
    print(f"[{_mark(base is not None)}] IS2 baseline "
          + ("미캡처(매수 전면 거부 상태)" if base is None
             else f"캡처됨({len(base)}종목 denylist)"))
    warns += (base is None)

    # ── 환경 새니티 ───────────────────────────────────────────────
    seed = os.environ.get("BOT_SEED_KRW")
    print(f"[{_mark(bool(seed))}] BOT_SEED_KRW " + ("설정" if seed else "미설정(사이징 0)"))
    if not kis.IS_MOCK:
        if os.environ.get("KIS_ORDERS_ENABLED") == "1":
            print("[✗] ⚠️ live 환경인데 KIS_ORDERS_ENABLED=1 — Stage 2 게이트 전 금지")
            fails += 1
        if os.environ.get("ALLOW_BUY") == "1":
            print("[✗] ⚠️ live 환경인데 ALLOW_BUY=1")
            fails += 1

    print(f"\n결과: {'GREEN' if fails == 0 else 'FAIL'} "
          f"(fail {fails} · warn {warns}) — "
          + ("매매 가능 상태" if fails == 0 else "신규 진입 금지 권고"))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
