"""실행 봇 — signals.json(두뇌)을 읽어 가드를 통과한 시그널만 매매(손).

사용:
  python -m bot.trader --once             # 한 번 실행(시그널 확인→매매 판단→종료)
  python -m bot.trader --loop             # POLL_SEC 간격 반복
  python -m bot.trader --status           # 보유/손익 현황만 출력
  python -m bot.trader --once --offline --signals public/api/signals.json
                                          # 네트워크 없이 로컬 파일로 로직 테스트

안전 가드(설정 settings.py):
  ① 가격 괴리: 현재가가 진입가 ±ENTRY_TOLERANCE 이내일 때만 매수
  ② 확정봉 모드: 전 거래일 시그널에도 있던 종목만(가짜 돌파 방지)
  ③ 동시 보유 MAX_POSITIONS 제한 / ④ 일일 실현손실 한도 도달 시 신규 중지
  ⑤ 기본 페이퍼 모드 — 실주문 없음. 토스 어댑터 구현 전엔 실계좌 불가 구조.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.request

from bot import settings as cfg
from bot import broker as brokers


# ── 상태 저장(포지션/일지/일일손익) ──────────────────────────────

def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return default


def _save(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(obj, fp, ensure_ascii=False, indent=1)


def load_state() -> dict:
    st = _load(cfg.STATE_PATH, {})
    st.setdefault("positions", {})   # code → {qty, entry, stop, target, ccy, name}
    st.setdefault("log", [])
    st.setdefault("realized", {})    # 날짜 → 실현손익(KRW)
    return st


# ── 시그널 ───────────────────────────────────────────────────────

def fetch_signals(url_or_path: str) -> dict:
    if url_or_path.startswith("http"):
        with urllib.request.urlopen(url_or_path + "?cb=" + str(int(time.time())),
                                    timeout=30) as resp:
            return json.load(resp)
    with open(url_or_path, encoding="utf-8") as fp:
        return json.load(fp)


def _krw(v: float, ccy: str) -> float:
    return v * cfg.FX_USDKRW if ccy == "USD" else v


def _fmt(v: float, ccy: str) -> str:
    return f"${v:,.2f}" if ccy == "USD" else f"{v:,.0f}원"


def _confirmed_filter(sigs: list[dict], today: str) -> list[dict]:
    """확정봉 모드: 전 거래일 시그널 목록에도 있던 (code, group)만 통과."""
    seen = _load(cfg.SEEN_PATH, {})
    today_keys = sorted({f'{s["code"]}:{s["group"]}' for s in sigs})
    prev_days = [d for d in sorted(seen) if d < today]
    prev = set(seen[prev_days[-1]]) if prev_days else set()
    seen[today] = today_keys
    for d in list(seen):                      # 오래된 기록 정리(7일)
        if d < today and d not in prev_days[-7:]:
            del seen[d]
    _save(cfg.SEEN_PATH, seen)
    if not prev:
        print("  (확정봉 모드: 이전 거래일 기록 없음 — 오늘은 관찰만, 매수 없음)")
        return []
    return [s for s in sigs if f'{s["code"]}:{s["group"]}' in prev]


# ── 매매 판단 ────────────────────────────────────────────────────

def manage_positions(st: dict, bk, today: str) -> None:
    """보유 종목 손절/목표 관리 — 손절 터치 시 전량, 목표 터치 시 전량 정리."""
    for code in list(st["positions"]):
        p = st["positions"][code]
        q = bk.quote(code, p["ccy"], fallback=p["entry"])
        if q is None:
            continue
        action = None
        if q <= p["stop"]:
            action = "손절"
        elif q >= p["target"]:
            action = "목표 도달 익절"
        if action:
            bk.sell(code, p["qty"], q)
            pnl = _krw((q - p["entry"]) * p["qty"], p["ccy"])
            st["realized"][today] = st["realized"].get(today, 0) + pnl
            st["log"].insert(0, {"t": today, "type": "sell", "code": code,
                                 "name": p["name"], "qty": p["qty"], "price": q,
                                 "reason": action, "pnl_krw": round(pnl)})
            print(f"  🔴 {action}: {p['name']}({code}) {p['qty']}주 @ "
                  f"{_fmt(q, p['ccy'])} → 실현 {pnl:+,.0f}원")
            del st["positions"][code]


def try_entries(st: dict, bk, sigs: list[dict], today: str) -> None:
    daily_pnl = st["realized"].get(today, 0)
    if daily_pnl <= -cfg.ACCOUNT_KRW * cfg.DAILY_LOSS_LIMIT:
        print(f"  ⛔ 일일 손실 한도 도달({daily_pnl:+,.0f}원) — 오늘 신규 매수 중지")
        return
    # 당일 매도(손절 포함)한 종목은 같은 날 재진입 금지 — 손절→재매수 반복(churn) 방지
    sold_today = {x["code"] for x in st["log"]
                  if x.get("type") == "sell" and x.get("t") == today}
    for s in sigs:
        code = s["code"]
        if s["group"] not in cfg.TRADE_GROUPS or code in st["positions"]:
            continue
        if code in sold_today:
            continue
        if len(st["positions"]) >= cfg.MAX_POSITIONS:
            print(f"  ⛔ 최대 보유({cfg.MAX_POSITIONS}종목) — 추가 매수 중지")
            break
        entry, stop, target, ccy = s["entry"], s["stop"], s["target"], s["ccy"]
        if not (entry and stop and entry > stop):
            continue
        q = bk.quote(code, ccy, fallback=s.get("price") or entry)
        if q is None:
            continue
        # ① 가격 괴리 가드 — 신호 계산 시점 대비 가격이 튀었으면 주문 금지
        dev = abs(q - entry) / entry
        if dev > cfg.ENTRY_TOLERANCE:
            print(f"  ⏸ {s['name']}({code}) 보류 — 현재가 {_fmt(q, ccy)}가 "
                  f"진입가 {_fmt(entry, ccy)}와 {dev*100:.1f}% 괴리(허용 "
                  f"{cfg.ENTRY_TOLERANCE*100:.1f}%)")
            continue
        # 수량 = 1회 리스크 예산 / 주당 손절폭(원화 환산)
        budget = cfg.ACCOUNT_KRW * cfg.RISK_PER_TRADE
        per_share = _krw(entry - stop, ccy)
        qty = int(budget // per_share) if per_share > 0 else 0
        if qty <= 0:
            print(f"  ⏸ {s['name']}({code}) 보류 — 손절폭이 리스크 예산보다 큼")
            continue
        bk.buy(code, qty, q)
        st["positions"][code] = {"qty": qty, "entry": q, "stop": stop,
                                 "target": target, "ccy": ccy, "name": s["name"]}
        st["log"].insert(0, {"t": today, "type": "buy", "code": code,
                             "name": s["name"], "qty": qty, "price": q,
                             "signal_id": s["id"]})
        print(f"  🟢 매수: {s['name']}({code}) {qty}주 @ {_fmt(q, ccy)} "
              f"(손절 {_fmt(stop, ccy)} · 목표 {_fmt(target, ccy)} · "
              f"저점권 {s.get('range_pos', 0)*100:.0f}%)")


def status(st: dict, bk) -> None:
    pos = st["positions"]
    if not pos:
        print("보유 없음.")
    for code, p in pos.items():
        q = bk.quote(code, p["ccy"], fallback=p["entry"]) or p["entry"]
        pl = (q / p["entry"] - 1) * 100
        print(f"  {p['name']}({code}) {p['qty']}주 | 평단 {_fmt(p['entry'], p['ccy'])} "
              f"→ 현재 {_fmt(q, p['ccy'])} ({pl:+.1f}%) | 손절 {_fmt(p['stop'], p['ccy'])}")
    total = sum(st["realized"].values())
    print(f"  누적 실현손익: {total:+,.0f}원")


def run_once(args) -> None:
    today = cfg.today_kst()
    bk = brokers.make(cfg.BROKER, offline=args.offline)
    st = load_state()
    data = fetch_signals(args.signals)
    sigs = data.get("signals", [])
    print(f"[{today}] 시그널 {len(sigs)}개 (생성 {data.get('generated_at')}) · "
          f"브로커={cfg.BROKER}{' (offline)' if args.offline else ''}")
    trade = [s for s in sigs if s["group"] in cfg.TRADE_GROUPS]
    if cfg.CONFIRMED_ONLY:
        trade = _confirmed_filter(trade, today)
        print(f"  확정봉 필터 통과: {len(trade)}개")
    manage_positions(st, bk, today)
    try_entries(st, bk, trade, today)
    status(st, bk)
    _save(cfg.STATE_PATH, st)


def main() -> None:
    ap = argparse.ArgumentParser(description="전환·저점 시그널 실행 봇(기본 페이퍼)")
    ap.add_argument("--once", action="store_true", help="1회 실행")
    ap.add_argument("--loop", action="store_true", help="반복 실행(POLL_SEC 간격)")
    ap.add_argument("--status", action="store_true", help="보유 현황만")
    ap.add_argument("--signals", default=cfg.SIGNALS_URL,
                    help="시그널 URL 또는 로컬 파일 경로")
    ap.add_argument("--offline", action="store_true",
                    help="시세 조회 없이 시그널가 사용(로직 테스트)")
    args = ap.parse_args()
    if args.status:
        status(load_state(), brokers.make(cfg.BROKER, offline=True))
        return
    if args.loop:
        while True:
            try:
                run_once(args)
            except Exception as e:                       # 루프는 죽지 않게
                print(f"[오류] {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(cfg.POLL_SEC)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
