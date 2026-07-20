"""매수 루프(Loop B) — autopaper 'now' 신호를 KIS 모의계좌에 **미러 매수**.

브레인/장부/손 아키텍처의 '손 - 매수' 쪽. 파수꾼(손절 매도)의 대칭:
  · autopaper(가상 장부)가 '지금 진입' 결정한 신호를 KIS 계좌에 실제(모의) 집행.
  · **브로커가 진실**: 매수 전 KIS 잔고(KR+미 3거래소 병합)를 재조회해
    이미 보유한 종목은 건너뛴다(중복매수 금지). 조회 실패=보수적 전면 skip
    (전역 포지션 캡을 검증할 수 없으면 이번 사이클은 안 산다 — fail-closed).
  · 가격 괴리 가드: 현재가가 신호 진입가 ±ENTRY_TOLERANCE 밖이면 skip(늦은 미러 방지).
  · autopaper 패리티 게이트(완전 미러, 2026-07-15): 어닝 D-3 이내 skip ·
    당일 매도(손절) 종목 재진입 금지(쿨다운) — 페이퍼 시뮬과 같은 규칙.
  · 롤아웃 캡 입력도 브로커-진실: 포지션 수(n_open)·투입원가(open_cost)를 잔고에서
    계산해 넘긴다(costbook 확정체결 배선 #25 전까지의 실측 소스). 사이클 안에서도
    매수마다 즉시 누적 — 같은 스냅샷으로 연속 매수해 SEED를 초과하는 구멍 차단.
  · 실제 전송은 kis_buy.execute_entry의 게이트 체인(ALLOW_BUY·kill·boot·SLA·
    rollout·ownership·ledger·sizing·place)을 전부 통과해야만. 이 모듈은 '무엇을
    시도할지'만.

이 모듈은 스스로 루프 돌지 않는다 — 서버 루프B(또는 검증)가 run_once를 주기 호출.
체결 확정은 kis_boot._resolve_acks(잔고대사)가 담당(매 사이클 자동).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.request

from bot import kis, kis_buy, kis_positions, settings

_US_EXCGS = ("NASD", "NYSE", "AMEX")   # 보유 병합용 — NYSE/AMEX 보유 누락 방지
_KST = datetime.timezone(datetime.timedelta(hours=9))


def _now_signals(signals: list[dict]) -> list[dict]:
    """'지금 진입'·신선·진입/손절 유효 신호만. 정렬: 갓전환·상위단계·상위점수."""
    cand = [s for s in signals
            if s.get("group") == "now" and s.get("fresh")
            and s.get("entry") and s.get("stop")]
    cand.sort(key=lambda s: (not s.get("fresh"), -s.get("stage", 0),
                             -s.get("norm", 0)))
    return cand


def _sold_today(fold: dict) -> set[str]:
    """오늘(KST) SELL 제출이 있는 종목들 — 당일 손절/청산 재진입 금지.
    autopaper 쿨다운(당일 손절 종목 재등장 금지 불변식)의 미러."""
    day = datetime.datetime.now(_KST).date()
    out: set[str] = set()
    for cur in fold.values():
        if (cur.get("side") or "").upper() != "SELL" or not cur.get("symbol"):
            continue
        ts = cur.get("submitted_at") or 0
        if ts and datetime.datetime.fromtimestamp(ts, _KST).date() == day:
            out.add(str(cur["symbol"]).upper())
    return out


def _broker_state(fx: float) -> tuple[dict, int, float, set[str]] | None:
    """미러 게이트 입력(브로커-진실): (보유맵, 봇 포지션 수, 봇 투입원가KRW, 당일매도).

    · 보유는 KR + 미 3거래소 **병합** — NYSE/AMEX 보유가 NASD 조회에 안 잡혀
      중복매수로 이어지는 구멍(검토 2026-07-15) 차단.
    · 봇 포지션 = 잔고 − baseline(사용자 기보유). 원장의 in-flight BUY(접수 후
      잔고 미반영 창)도 수에 가산 — ack 직후 과다 진입 방지.
    · 어느 조회든 실패(None)면 전체 None — 전역 캡 검증 불가 = 이번 사이클 안 산다.
    """
    from bot import ledger, ownership
    rows: dict[str, dict] = {}
    kr = kis.positions_detail("KR")
    if kr is None:
        return None
    for p in kr:
        rows.setdefault(p["code"], p)
    for excg in _US_EXCGS:
        us = kis.positions_detail("US", excg=excg)
        if us is None:
            return None
        for p in us:
            rows.setdefault(p["code"], p)
    held = {c: int(p["qty"]) for c, p in rows.items()}
    base = ownership.baseline() or set()
    bot_rows = [p for c, p in rows.items() if c not in base]
    n_open = len(bot_rows)
    open_cost = sum(float(p.get("buy_amt") or 0)
                    * (1.0 if p.get("market") == "KR" else fx)
                    for p in bot_rows)
    #   in-flight BUY(접수 후 잔고 미반영) — 포지션 수 AND 투입원가에 둘 다 가산.
    #   (감사 수정 #5: 예전엔 수에만 넣고 원가엔 안 넣어, 총량 게이트가 미체결 매수를
    #    못 봐 SEED 초과 배포 가능했다.) 원가는 원장 기록가×수량(원화 환산).
    fold = ledger._fold()
    inflight_syms: set[str] = set()
    for v in fold.values():
        if (v.get("side") or "").upper() != "BUY" or v.get("state") not in ledger._INFLIGHT:
            continue
        s = str(v.get("symbol") or "").upper()
        if not s or s in held:
            continue
        inflight_syms.add(s)
        try:
            q = int(v.get("intended") or 0)
            px = float(v.get("price") or 0)
            mk = v.get("market") or kis.market_of_symbol(s)
            open_cost += q * px * (1.0 if mk == "KR" else fx)
        except (TypeError, ValueError):
            pass
    n_open += len(inflight_syms)
    return held, n_open, open_cost, _sold_today(fold)


def run_once(signals: list[dict], *, fx: float | None = None,
             excg_of: dict | None = None, reason: str = "미러진입") -> list[dict]:
    """'now' 신호를 KIS에 미러 매수 시도. 반환: 종목별 {code, gate, ok?, qty?, why}.

    fx: USD→KRW 환율(미국주 원화 사이징용). None이면 settings.FX_USDKRW.
    excg_of: {code: 'NASD'|'NYSE'|'AMEX'} 미국 거래소 매핑(없으면 NASD).
    """
    fx = float(fx or settings.FX_USDKRW)
    excg_of = excg_of or {}
    results: list[dict] = []

    # 1차 게이트(브로커 조회 전) — 세션·어닝. 후보가 없으면 잔고 조회도 안 한다.
    cand: list[dict] = []
    for s in _now_signals(signals):
        code = str(s["code"]).upper()
        if not settings.market_open(s.get("ccy", "USD")):
            results.append({"code": code, "gate": "session", "why": "장 아님"})
            continue
        ed = s.get("earnings_d")
        try:
            ed = float(ed) if ed is not None else None
        except (TypeError, ValueError):
            ed = None
        if ed is not None and 0 <= ed <= 3:        # autopaper와 동일 규칙(갭 리스크)
            results.append({"code": code, "gate": "earnings",
                            "why": f"어닝 D-{int(ed)} 이내 — 신규 진입 금지"})
            continue
        cand.append(s)
    if not cand:
        return results

    st = _broker_state(fx)
    if st is None:                                 # 잔고 불명 → 보수적으로 안 산다
        for s in cand:
            results.append({"code": str(s["code"]).upper(), "gate": "holdings",
                            "why": "잔고 조회실패/불완전 — skip"})
        return results
    held, n_open, open_cost, sold_today = st

    for s in cand:
        code = str(s["code"]).upper()
        ccy = s.get("ccy", "USD")
        market = kis.market_of_ccy(ccy)
        excg = excg_of.get(code, "NASD")

        if code in held:                           # 브로커-진실: 이미 보유 = 중복 금지
            results.append({"code": code, "gate": "already",
                            "why": f"이미 KIS 보유 {held[code]}주"}); continue
        if code in sold_today:                     # 당일 손절 종목 재진입 금지(패리티)
            results.append({"code": code, "gate": "cooldown",
                            "why": "당일 매도 종목 — 재진입 쿨다운"}); continue
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
        # 브로커-진실 캡 입력: n_open(수량 캡)·open_cost(총량 게이트) + 사이클 내
        #   즉시 누적. hldg_before=0(위 already 게이트로 신규 진입만 옴 — ack 대사 기준).
        d = kis_buy.execute_entry(pos_key, code, price_usd=cur,
                                  per_share_risk_usd=per_share, krw_per_usd=fx,
                                  excg=excg, market=market, reason=reason,
                                  open_positions=n_open, open_cost_krw=open_cost,
                                  hldg_before=0)
        if d.ok:
            # 파수꾼이 feed에 없어도 이 손절선으로 보호하도록 기록(브로커-진실 fallback).
            kis_positions.record(code, stop=float(s["stop"]), ccy=ccy,
                                 entry=entry, qty=d.qty, name=s.get("name", ""),
                                 opened=settings.today_kst())
            # 사이클 내 상태 갱신 — 같은 스냅샷으로 연속 매수해 캡/SEED를 뚫는 것 방지.
            held[code] = d.qty
            n_open += 1
            open_cost += d.qty * cur * (1.0 if market == "KR" else fx)
            # KIS 모의계좌 실매수 알림(텔레그램) — 사용자 요청: 실계좌 매수/매도만.
            try:
                from bot import notify
                u = "원" if market == "KR" else "$"
                notify.send(
                    f"🟢 <b>KIS 매수</b> — {s.get('name', code)}({code})\n"
                    f"  {d.qty}주 @ {cur}{u} · 손절 {s['stop']}{u}", critical=True)
            except Exception:
                pass
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
    # 부팅 대사 — 이 프로세스의 매매 게이트(kis_boot.trading_allowed)를 연다.
    #   파수꾼과 별개 프로세스라 매수 루프도 자체적으로 돌려야 boot 게이트가 열린다.
    #   UNKNOWN 0건이면 원장 읽기만(가벼움) + ack(접수)→체결 잔고대사도 함께 수행.
    #   실패해도 게이트 닫힌 채 진행(fail-closed).
    try:
        from bot import kis_boot
        kis_boot.boot_reconcile()
    except Exception as e:
        print(f"[부팅 대사 오류] {type(e).__name__}: {e}", flush=True)
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
