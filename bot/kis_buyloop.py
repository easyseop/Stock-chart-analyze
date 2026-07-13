"""매수 루프(Loop B) — autopaper 'now' 신호를 KIS 모의계좌에 **미러 매수**.

브레인/장부/손 아키텍처의 '손 - 매수' 쪽. 파수꾼(손절 매도)의 대칭:
  · autopaper(가상 장부)가 '지금 진입' 결정한 신호를 KIS 계좌에 실제(모의) 집행.
  · **브로커가 진실**: 매수 전 KIS 잔고를 재조회해 **이미 보유한 종목은 건너뛴다**
    (autopaper가 이미 반영된 것 = 중복매수 금지). 잔고 조회 실패=보수적 skip.
  · 가격 괴리 가드: 현재가가 신호 진입가 ±ENTRY_TOLERANCE 밖이면 skip(늦은 미러 방지).
  · 실제 전송은 kis_buy.execute_entry의 **9중 게이트**(ALLOW_BUY·kill·boot·SLA·
    rollout·ownership·ledger·sizing·place)를 전부 통과해야만. 이 모듈은 '무엇을 시도할지'만.

이 모듈은 스스로 루프 돌지 않는다 — 서버 루프B(또는 검증)가 run_once를 주기 호출.
체결 확정→costbook 반영은 후속(별도)에서 잔고 폴링으로 붙인다.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

from bot import kis, kis_buy, settings


def _now_signals(signals: list[dict]) -> list[dict]:
    """'지금 진입'·신선·진입/손절 유효 신호만. 정렬: 갓전환·상위단계·상위점수."""
    cand = [s for s in signals
            if s.get("group") == "now" and s.get("fresh")
            and s.get("entry") and s.get("stop")]
    cand.sort(key=lambda s: (not s.get("fresh"), -s.get("stage", 0),
                             -s.get("norm", 0)))
    return cand


def run_once(signals: list[dict], *, fx: float | None = None,
             excg_of: dict | None = None, reason: str = "미러진입") -> list[dict]:
    """'now' 신호를 KIS에 미러 매수 시도. 반환: 종목별 {code, gate, ok?, qty?, why}.

    fx: USD→KRW 환율(미국주 원화 사이징용). None이면 settings.FX_USDKRW.
    excg_of: {code: 'NASD'|'NYSE'|'AMEX'} 미국 거래소 매핑(없으면 NASD).
    """
    fx = float(fx or settings.FX_USDKRW)
    excg_of = excg_of or {}
    results: list[dict] = []
    hcache: dict[str, dict | None] = {}          # 시장별 잔고 1회 조회 캐시

    def _held(market: str, excg: str):
        if market not in hcache:
            hcache[market] = kis.holdings(market, excg=excg)
        return hcache[market]

    for s in _now_signals(signals):
        code = str(s["code"]).upper()
        ccy = s.get("ccy", "USD")
        market = kis.market_of_ccy(ccy)
        excg = excg_of.get(code, "NASD")

        if not settings.market_open(ccy):
            results.append({"code": code, "gate": "session", "why": "장 아님"}); continue
        h = _held(market, excg)
        if h is None:                             # 잔고 불명 → 보수적으로 안 산다
            results.append({"code": code, "gate": "holdings",
                            "why": "잔고 조회실패/불완전 — skip"}); continue
        if code in h:                             # 브로커-진실: 이미 보유 = 중복 금지
            results.append({"code": code, "gate": "already",
                            "why": f"이미 KIS 보유 {h[code]}주"}); continue
        cur = kis.last_price(code, market=market, excg=excg)
        if not cur or cur <= 0:
            results.append({"code": code, "gate": "quote", "why": "현재가 조회 실패"}); continue
        entry = float(s["entry"])
        if entry <= 0 or abs(cur - entry) / entry > settings.ENTRY_TOLERANCE:
            results.append({"code": code, "gate": "tolerance",
                            "why": f"가격 괴리 {cur} vs 진입 {entry}"}); continue
        per_share = entry - float(s["stop"])
        if per_share <= 0:
            results.append({"code": code, "gate": "input", "why": "손절폭 무효"}); continue

        pos_key = f"kb:{s.get('id') or code}"
        d = kis_buy.execute_entry(pos_key, code, price_usd=cur,
                                  per_share_risk_usd=per_share, krw_per_usd=fx,
                                  excg=excg, market=market, reason=reason)
        results.append({"code": code, "gate": d.gate, "ok": d.ok,
                        "qty": d.qty, "why": d.why})
    return results


def _fetch_signals() -> list[dict]:
    for url in settings.SIGNALS_SOURCES:
        try:
            with urllib.request.urlopen(
                    url + "?cb=" + str(os.getpid()), timeout=15) as r:
                return (json.load(r) or {}).get("signals", [])
        except Exception:
            continue
    return []


def _cycle() -> None:
    sigs = _fetch_signals()
    print(f"신호 {len(sigs)}건 로드 · 'now' 후보 {len(_now_signals(sigs))}건", flush=True)
    for r in run_once(sigs):
        mark = "✓ 전송" if r.get("ok") else "·"
        print(f"  {mark} {r['code']} [{r['gate']}] {r.get('why', '')}", flush=True)


def main() -> int:
    import argparse
    import time
    ap = argparse.ArgumentParser(description="KIS 미러 매수 루프(기본 1회)")
    ap.add_argument("--loop", action="store_true", help="POLL초마다 반복(서버 모드)")
    ap.add_argument("--poll", type=int, default=300, help="반복 주기(초, 기본 300)")
    args = ap.parse_args()
    if not args.loop:
        _cycle()
        return 0
    print(f"매수 루프 시작 — {args.poll}초 주기(ALLOW_BUY·KIS_ORDERS_ENABLED 필요)",
          flush=True)
    while True:
        try:
            _cycle()
        except Exception as e:                     # 루프는 죽지 않는다
            print(f"[오류] {type(e).__name__}: {e}", flush=True)
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
