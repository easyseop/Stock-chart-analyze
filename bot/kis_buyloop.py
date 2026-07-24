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


def _broker_state(fx: float):
    """미러 게이트 입력(브로커-진실). 반환:
       (held, held_cost{code:원가KRW}, inflight{sym:(원가KRW, sleeve)}, sold_today)
    또는 조회 실패 시 None.

    · 보유는 KR + 미 3거래소 병합(중복매수 구멍 차단).
    · held_cost = 잔고 − baseline(사용자 기보유)의 종목별 투입원가(원화).
    · inflight = 원장의 in-flight BUY(접수 후 잔고 미반영). sleeve는 pos_key
      접두("sb:"=B, 그 외=A)로 판별 — 슬리브별 예산 분리(A/B가 서로 잠식 방지).
    · 종목별로 반환해 호출부가 슬리브별로 n_open·open_cost를 파티션한다.
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
    held_cost = {c: float(p.get("buy_amt") or 0)
                 * (1.0 if p.get("market") == "KR" else fx)
                 for c, p in rows.items() if c not in base}
    fold = ledger._fold()
    inflight: dict[str, tuple[float, str]] = {}
    for k, v in fold.items():
        if (v.get("side") or "").upper() != "BUY" or v.get("state") not in ledger._INFLIGHT:
            continue
        s = str(v.get("symbol") or "").upper()
        if not s or s in held:
            continue
        try:
            q = int(v.get("intended") or 0)
            px = float(v.get("price") or 0)
            mk = v.get("market") or kis.market_of_symbol(s)
            cost = q * px * (1.0 if mk == "KR" else fx)
        except (TypeError, ValueError):
            cost = 0.0
        sleeve = "B" if str(k).startswith("sb:") else "A"
        inflight[s] = (cost, sleeve)
    return held, held_cost, inflight, _sold_today(fold)


def _partition(held_cost: dict, inflight: dict, sleeve: str,
               b_codes: set) -> tuple[int, float]:
    """슬리브별 (봇 포지션 수, 투입원가KRW). b_codes=현재 B가 보유한 종목 집합.
       held 종목은 b_codes로, inflight는 원장 sleeve 태그로 귀속."""
    def _mine(code):
        return (code in b_codes) if sleeve == "B" else (code not in b_codes)
    n = 0
    cost = 0.0
    for c, cst in held_cost.items():
        if _mine(c):
            n += 1
            cost += cst
    for s, (cst, sl) in inflight.items():
        if sl == sleeve:
            n += 1
            cost += cst
    return n, cost


def _b_held_codes(held: dict) -> set:
    """현재 보유 중 슬리브 B가 산 종목 집합(kis_positions 태그 기준)."""
    try:
        recs = kis_positions.load()
    except Exception:
        return set()
    return {c for c, info in recs.items()
            if info.get("sleeve") == "B" and c in held}


def _shelf_cands(signals: list[dict]) -> list[dict]:
    """매물대 반등(B) 후보 — group='shelf'·진입/손절 유효. 손익비 높은 순."""
    c = [s for s in signals if s.get("group") == "shelf"
         and s.get("entry") and s.get("stop")]
    c.sort(key=lambda s: -float((s.get("shelf") or {}).get("rr") or 0))
    return c


def run_once(signals: list[dict], *, fx: float | None = None,
             excg_of: dict | None = None, reason: str = "미러진입",
             sleeve: str = "A", group: str = "now",
             seed_krw: float | None = None) -> list[dict]:
    """신호를 KIS에 미러 매수 시도. 반환: 종목별 {code, gate, ok?, qty?, why}.

    sleeve/group: 'A'/'now'=전환확정(기본) · 'B'/'shelf'=매물대 반등(별도 예산).
    seed_krw: 이 슬리브 전용 SEED(None이면 execute_entry가 기본 BOT_SEED_KRW).
    fx: USD→KRW 환율. excg_of: {code: 거래소}.
    """
    fx = float(fx or settings.FX_USDKRW)
    excg_of = excg_of or {}
    results: list[dict] = []

    src = _shelf_cands(signals) if group == "shelf" else _now_signals(signals)
    # 1차 게이트(브로커 조회 전) — 세션·어닝. 후보가 없으면 잔고 조회도 안 한다.
    cand: list[dict] = []
    for s in src:
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
    held, held_cost, inflight, sold_today = st
    b_codes = _b_held_codes(held)
    #   슬리브별 파티션 — A/B가 서로의 종목 수·투입원가를 세지 않게(예산 잠식 방지).
    n_open, open_cost = _partition(held_cost, inflight, sleeve, b_codes)
    prefix = "sb:" if sleeve == "B" else "kb:"

    for s in cand:
        code = str(s["code"]).upper()
        ccy = s.get("ccy", "USD")
        market = kis.market_of_ccy(ccy)
        excg = excg_of.get(code, "NASD")

        if code in held:                           # 브로커-진실: 이미 보유 = 중복 금지(A·B 공통)
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

        pos_key = f"{prefix}{s.get('id') or code}"
        d = kis_buy.execute_entry(pos_key, code, price_usd=cur,
                                  per_share_risk_usd=per_share, krw_per_usd=fx,
                                  excg=excg, market=market, reason=reason,
                                  open_positions=n_open, open_cost_krw=open_cost,
                                  hldg_before=0, seed_krw=seed_krw)
        if d.ok:
            # 파수꾼이 feed에 없어도 이 손절선으로 보호(브로커-진실 fallback) + 슬리브 태그.
            kis_positions.record(code, stop=float(s["stop"]), ccy=ccy,
                                 entry=entry, qty=d.qty, name=s.get("name", ""),
                                 opened=settings.today_kst(), sleeve=sleeve)
            held[code] = d.qty
            n_open += 1
            open_cost += d.qty * cur * (1.0 if market == "KR" else fx)
            if sleeve == "B":
                b_codes.add(code)
            try:
                from bot import notify
                u = "원" if market == "KR" else "$"
                tag = " (매물대B)" if sleeve == "B" else ""
                notify.send(
                    f"🟢 <b>KIS 매수{tag}</b> — {s.get('name', code)}({code})\n"
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
    print(f"신호 {len(sigs)}건 로드 · 'now' {len(_now_signals(sigs))}건 · "
          f"'shelf' {len(_shelf_cands(sigs))}건", flush=True)
    for r in run_once(sigs):                          # 슬리브 A(전환확정)
        mark = "✓ 전송" if r.get("ok") else "·"
        print(f"  {mark} {r['code']} [{r['gate']}] {r.get('why', '')}", flush=True)
    # 슬리브 B(매물대 반등) — BOT_SEED_SB_KRW 설정 시에만(별도 예산 테스트).
    from bot import envelope
    sb = envelope.seed_krw_sb()
    if sb > 0:
        for r in run_once(sigs, sleeve="B", group="shelf", seed_krw=sb,
                          reason="매물대B"):
            mark = "✓ 전송" if r.get("ok") else "·"
            print(f"  [B] {mark} {r['code']} [{r['gate']}] {r.get('why', '')}",
                  flush=True)
    # 성과 vs 지수 추적(알파) — 실패해도 매매에 영향 0(무해).
    try:
        from bot import alpha
        alpha.tick()
    except Exception as e:
        print(f"[알파 추적 오류] {type(e).__name__}: {e}", flush=True)


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
