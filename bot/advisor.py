"""알림 조언 봇 — 자동 주문 없이 매수/매도 '제안'만 텔레그램으로 전송.

두 알림 스트림:
  ① 매수 제안 — signals.json의 '지금 진입'(now) 그룹에 새로 들어온 종목.
     (신규만 — 재빌드마다 같은 종목 중복 알림 방지, alert_state로 추적)
  ② 매도 제안 — holdings.json(사용자가 직접 기록한 보유 종목)의 현재가가
     손절/목표에 닿으면. holdings.json은 git 추적(직접 편집·커밋).

"API만 연결하면 자동매매 가능"의 의미: 이 파일의 notify() 호출 지점이 바로
나중에 broker.buy()/sell()로 바뀔 지점 — 판단 로직(gates.classify)은 이미
자동매매와 동일. 지금은 알림만, 나중은 buy/sell만 추가하면 된다.

사용:
  python -m bot.advisor --once
  python -m bot.advisor --once --signals public/api/signals.json --holdings holdings.json --dry-run
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import time
import urllib.request

from bot import notify
from bot import settings as cfg

STATE_PATH = "bot/alert_state.json"


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


def fetch_signals(url_or_path: str) -> dict:
    if url_or_path.startswith("http"):
        with urllib.request.urlopen(
                url_or_path + "?cb=" + str(int(time.time())), timeout=30) as resp:
            return json.load(resp)
    with open(url_or_path, encoding="utf-8") as fp:
        return json.load(fp)


def _fmt(v: float, ccy: str) -> str:
    return f"${v:,.2f}" if ccy == "USD" else f"{v:,.0f}원"


def _quote(code: str, fallback: float | None) -> float | None:
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader(code)
        return float(df["Close"].iloc[-1])
    except Exception:
        return fallback


# ── ① 매수 제안 ──────────────────────────────────────────────────

def check_buy_signals(sigs: list[dict], state: dict, dry_run: bool) -> int:
    sent = state.setdefault("sent_buy", [])
    sent_set = set(sent)
    n = 0
    for s in sigs:
        if s["group"] != "now" or s["id"] in sent_set:
            continue
        rp = s.get("range_pos")
        rp_txt = f" · 📍저점권 {rp*100:.0f}%" if rp is not None else ""
        gap = s.get("break_gap")
        fresh_txt = ("\n🔥 갓 전환 — 추세선 위 "
                     f"+{gap*100:.1f}%" if s.get("fresh") and gap is not None
                     else ("\n↗ 돌파후 진행(승률↓ 참고)" if s.get("fresh") is False
                           and s.get("stage", 0) >= 3 else ""))
        text = (
            f"🟢 <b>매수 제안</b> — {s['name']}({s['code']})\n"
            f"단계 {s.get('stage', 0)}{rp_txt}{fresh_txt}\n"
            f"진입 <b>{_fmt(s['entry'], s['ccy'])}</b> · "
            f"손절 {_fmt(s['stop'], s['ccy'])} · "
            f"목표 {_fmt(s['target'], s['ccy'])}\n"
            f"참고수량(계좌1%리스크): {s.get('shares_1pct', '-')}주\n"
            f"⚠️ 차트 기준 제안 · 투자권유 아님. 주문 전 실시간가 재확인."
        )
        if not dry_run:
            notify.send(text)
        else:
            print(text)
        sent.append(s["id"])
        n += 1
    state["sent_buy"] = sent[-500:]      # 무한 성장 방지
    return n


# ── ② 매도 제안(손절/목표) ───────────────────────────────────────

def check_sell_alerts(holdings: list[dict], state: dict, dry_run: bool) -> int:
    today = datetime.date.today().isoformat()
    sent_today = state.setdefault("sent_sell", {}).setdefault(today, [])
    # 오래된 날짜 정리(7일 초과)
    for d in list(state["sent_sell"]):
        if d < (datetime.date.today() - datetime.timedelta(days=7)).isoformat():
            del state["sent_sell"][d]
    n = 0
    for h in holdings:
        code, ccy = h["code"], h.get("ccy", "USD")
        stop, target = h.get("stop"), h.get("target")
        key = f'{code}:{today}'
        if key in sent_today:
            continue
        px = _quote(code, h.get("avg"))
        if px is None:
            continue
        reason = None
        if stop and px <= stop:
            reason = "손절"
        elif target and px >= target:
            reason = "목표 도달(익절)"
        if reason:
            pl = (px / h["avg"] - 1) * 100 if h.get("avg") else 0
            text = (
                f"🔴 <b>매도 제안 — {reason}</b> — {h.get('name', code)}({code})\n"
                f"보유 {h.get('qty', '-')}주 · 평단 {_fmt(h['avg'], ccy)} → "
                f"현재 {_fmt(px, ccy)} ({pl:+.1f}%)\n"
                f"손절선 {_fmt(stop, ccy) if stop else '-'} · "
                f"목표선 {_fmt(target, ccy) if target else '-'}\n"
                f"⚠️ 차트 기준 제안 · 투자권유 아님."
            )
            if not dry_run:
                notify.send(text)
            else:
                print(text)
            sent_today.append(key)
            n += 1
    return n


def run_once(args) -> None:
    state = _load(STATE_PATH, {})
    data = fetch_signals(args.signals)
    sigs = data.get("signals", [])
    holdings = _load(args.holdings, [])
    nb = check_buy_signals(sigs, state, args.dry_run)
    ns = check_sell_alerts(holdings, state, args.dry_run)
    print(f"시그널 {len(sigs)}개 · 보유 {len(holdings)}종목 → "
          f"매수제안 {nb}건 · 매도제안 {ns}건 전송")
    _save(STATE_PATH, state)


def main() -> None:
    ap = argparse.ArgumentParser(description="매수/매도 제안 알림 봇(텔레그램)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--signals", default=cfg.SIGNALS_URL)
    ap.add_argument("--holdings", default="holdings.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="텔레그램 전송 없이 콘솔에만 출력")
    args = ap.parse_args()
    run_once(args)


if __name__ == "__main__":
    main()
