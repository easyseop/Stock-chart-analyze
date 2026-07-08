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


def fetch_signals_chain(primary: str) -> tuple[dict, str]:
    """신호 조회 — 기본 URL이면 feed(state 브랜치)→Pages 순 폴백 체인.
    Pages 장애 때 낡은 신호로 제안이 나가는 결합을 끊는다(기계용 feed 분리).
    반환: (데이터, 실제 사용한 소스)."""
    sources = (cfg.SIGNALS_SOURCES if primary == cfg.SIGNALS_URL
               else (primary,))
    last_err = None
    for src in sources:
        try:
            return fetch_signals(src), src
        except Exception as e:
            last_err = e
    raise last_err


def _held_codes() -> set:
    """자동매매 계좌가 이미 보유·대기 중인 종목코드 집합.
    이미 산 종목엔 '지금 진입 자리!'·'돌파 발생' 제안이 무의미·혼란 → 알림에서 제외.
    조회 실패는 빈 집합(제외 안 함 — 알림을 막기보다 중복을 감수. 보수적)."""
    for src in cfg.PAPER_SOURCES:
        try:
            d = fetch_signals(src)   # 같은 http/파일 로더 재사용
            codes = set()
            for p in d.get("positions", []):
                if p.get("code"):
                    codes.add(p["code"])
            for o in d.get("pending", []):
                if o.get("code"):
                    codes.add(o["code"])
            return codes
        except Exception:
            continue
    return set()


def _signals_age_min(data: dict) -> float | None:
    """generated_at 기준 신호 나이(분). 파싱 실패 시 None(판정 보류)."""
    try:
        import datetime
        t = datetime.datetime.fromisoformat(data.get("generated_at", ""))
        return (datetime.datetime.now(t.tzinfo) - t).total_seconds() / 60
    except Exception:
        return None


def _fmt(v: float, ccy: str) -> str:
    return f"${v:,.2f}" if ccy == "USD" else f"{v:,.0f}원"


def _quote(code: str, fallback: float | None) -> float | None:
    # 1순위: 토스 시세(키 있을 때만·실패 시 조용히 폴백) — 실전급 최신가.
    try:
        from bot import toss
        px = toss.quote_price(code)
        if px is not None:
            return px
    except Exception:
        pass
    # 폴백: FDR(야후/Stooq) 마지막 종가.
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader(code)
        return float(df["Close"].iloc[-1])
    except Exception:
        return fallback


# ── 알림 폭주 방지: 하루 총량 서킷브레이커 ───────────────────────
#   회당 상한(BUY 8·ARRIVAL 6)은 '1회 실행'만 막는다. 밤새 여러 번 실행되면
#   누적으로 쏟아질 수 있어(필터가 깨지는 최악의 경우 특히) 하루 총량도 막는다.

def _sent_today(state: dict) -> int:
    return state.setdefault("day_sent", {}).get(cfg.today_kst(), 0)


def _record_sent(state: dict, n: int) -> None:
    if n <= 0:
        return
    dk = state.setdefault("day_sent", {})
    today = cfg.today_kst()
    dk[today] = dk.get(today, 0) + n
    for d in list(dk):                    # 오래된 날짜 정리(3일 초과)
        if d < cfg.days_ago_kst(3):
            del dk[d]


# ── ① 매수 제안 ──────────────────────────────────────────────────

def check_buy_signals(sigs: list[dict], state: dict, dry_run: bool,
                      budget: int = 10 ** 9, held: set | None = None) -> int:
    held = held or set()
    sent = state.setdefault("sent_buy", [])
    sent_set = set(sent)
    # '지금 살 수 있는' 종목만(장 열린 시장) + 새 신호만 → 우선순위 높은 것부터 상한까지.
    #   (한국 낮에 미국주 제안이 우르르 오던 폭주 방지 — 지금 열린 시장만 알림)
    #   자동매매가 이미 보유·대기 중인 종목은 제외 — 이미 산 걸 "매수 제안"하면 혼란(사용자 지적).
    cand = [s for s in sigs
            if s["group"] == "now" and s["id"] not in sent_set
            and s["code"] not in held
            and cfg.market_open(s["ccy"])]
    cand.sort(key=lambda s: (not s.get("fresh"), -s.get("stage", 0),
                             -s.get("norm", 0)))          # 갓전환·상위단계 먼저
    cap = max(0, min(cfg.BUY_ALERT_MAX, budget))          # 회당·일일 상한 중 작은 쪽
    n = 0
    for s in cand[:cap]:
        # 간단히 — 종목명 + 진입/손절 + 전술 한 줄. (상세는 웹 차트에서)
        tag = "🔥" if s.get("fresh") else "↗"
        t = s.get("tactic") or {}
        tac = f" · {t['label']}" if t.get("label") else ""
        ed = s.get("earnings_d")
        earn = " · ⚠️어닝임박" if (ed is not None and 0 <= ed <= 3) else ""
        text = (
            f"🟢 <b>[관찰] 매수 제안</b> {tag} {s['name']}({s['code']})\n"
            f"진입 {_fmt(s['entry'], s['ccy'])} · 손절 {_fmt(s['stop'], s['ccy'])}{tac}{earn}\n"
            f"— 자동매매 미체결(제안만). 실제 체결은 '🤖 자동매매 체결' 알림.\n"
            f'<a href="{cfg.SITE_URL}/stocks/{s["code"]}.html">📈 상세</a>'
        )
        if not cfg.market_open(s["ccy"]):    # 전송 직전 재확인(장 닫힘 오발송 방지)
            continue
        if dry_run:
            print(text)
            ok = True
        else:
            ok = notify.send(text)       # 전송 성공 시에만 '보냈음' 기록 —
        if ok:                            #   토큰 등록 전 신호가 소실되지 않게
            sent.append(s["id"])
            n += 1
    state["sent_buy"] = sent[-500:]      # 무한 성장 방지
    return n


# ── ①-b 눌림/돌파 '도달' 알림 — 사용자 전략의 핵심 순간 ─────────
# '곧 올 자리(watch)' 종목이 실제로 진입 구간에 닿는 순간을 알림.
#   눌림 대기(pullback): 현재가가 진입가까지 내려옴 → "기다리던 자리 도착"
#   돌파 대기(breakout): 현재가가 진입가(저항) 위로 올라섬 → "돌파 발생"

ARRIVAL_TOL = 0.01   # 진입가 ±1% 이내면 '도달'로 판정


def check_arrivals(sigs: list[dict], state: dict, dry_run: bool,
                   budget: int = 10 ** 9, held: set | None = None) -> int:
    held = held or set()
    today = cfg.today_kst()
    sent = state.setdefault("sent_arrival", {}).setdefault(today, [])
    for d in list(state["sent_arrival"]):
        if d < cfg.days_ago_kst(7):
            del state["sent_arrival"][d]
    # 지금 장 열린 시장의 watch 종목만(한국 낮에 미국주 도달알림 폭주 방지) +
    #   상한까지. 우선순위: 갓전환·상위단계 먼저.
    #   자동매매가 이미 보유·대기 중인 종목은 제외 — 이미 산 종목의 '돌파 발생'은
    #   중복·혼란(사용자가 MRSH 사례로 지적: 이미 보유인데 돌파 알림이 옴).
    cand = [s for s in sigs
            if s["group"] == "watch"
            and s.get("entry_kind") in ("pullback", "breakout")
            and s["code"] not in sent
            and s["code"] not in held
            and s.get("entry")
            and cfg.market_open(s["ccy"])]
    cand.sort(key=lambda s: (not s.get("fresh"), -s.get("stage", 0)))
    cap = max(0, min(cfg.ARRIVAL_ALERT_MAX, budget))   # 회당·일일 상한 중 작은 쪽
    n = 0
    for s in cand:
        if n >= cap:
            break
        if not cfg.market_open(s["ccy"]):              # 전송 직전 재확인
            continue
        code = s["code"]
        entry = s.get("entry")
        px = _quote(code, None)
        if px is None:
            continue
        hit, title = None, ""
        if s["entry_kind"] == "pullback" and px <= entry * (1 + ARRIVAL_TOL):
            hit, title = True, "눌림 도달 — 기다리던 자리"
        elif s["entry_kind"] == "breakout" and px >= entry * (1 - ARRIVAL_TOL):
            hit, title = True, "돌파 발생 — 저항 넘김"
        if hit:
            text = (
                f"🎯 <b>[관찰] {title}</b> {s['name']}({code})\n"
                f"현재가 {_fmt(px, s['ccy'])} · 손절 {_fmt(s['stop'], s['ccy'])}\n"
                f"— 자동매매 미보유 종목의 도달 알림(제안). 체결과 무관.\n"
                f'<a href="{cfg.SITE_URL}/stocks/{code}.html">📈 상세</a>'
            )
            ok = True if dry_run else notify.send(text)
            if dry_run:
                print(text)
            if ok:
                sent.append(code)
                n += 1
    return n


# ── ② 매도 제안(손절/목표) ───────────────────────────────────────

def check_sell_alerts(holdings: list[dict], state: dict, dry_run: bool) -> int:
    today = cfg.today_kst()
    sent_today = state.setdefault("sent_sell", {}).setdefault(today, [])
    # 오래된 날짜 정리(7일 초과)
    for d in list(state["sent_sell"]):
        if d < cfg.days_ago_kst(7):
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
                # 손절 터치는 P0(ntfy 이중화) · 목표 도달(익절)은 좋은 소식이라 텔레그램만.
                notify.send(text, critical=(reason == "손절"))
            else:
                print(text)
            sent_today.append(key)
            n += 1
    return n


def run_once(args) -> None:
    state = _load(STATE_PATH, {})
    data, src = fetch_signals_chain(args.signals)
    sigs = data.get("signals", [])
    holdings = _load(args.holdings, [])
    # stale 가드 — 장중인데 신호가 낡았으면(빌드 정체 등) 제안을 보내지 않는다.
    #   낡은 가격 기준의 '지금 진입' 제안은 없느니만 못함. 대신 하루 1회만 경보.
    age = _signals_age_min(data)
    stale = (age is not None and age > cfg.SIGNALS_STALE_MIN
             and (cfg.market_open("USD") or cfg.market_open("KRW")))
    if stale:
        print(f"신호 {age:.0f}분 낡음(>{cfg.SIGNALS_STALE_MIN}) — 제안 생략 [src={src}]")
        if (state.get("stale_notice_day") != cfg.today_kst()
                and not args.dry_run):
            state["stale_notice_day"] = cfg.today_kst()
            notify.send(f"⚠️ 신호가 {age:.0f}분째 낡음 — 매수/도달 제안을 일시 중단"
                        f"(낡은 가격 기준 제안 방지). 빌드 복구 시 자동 재개.",
                        critical=True)
        nb = na = 0
    else:
        # 하루 총량 서킷브레이커 — 매수/도달 제안에만 적용(매도는 보유 관리라 예외).
        #   자동매매 보유·대기 종목을 1회 조회해 두 알림에서 함께 제외(중복·혼란 방지).
        held = _held_codes()
        budget = max(0, cfg.DAILY_ALERT_BUDGET - _sent_today(state))
        nb = check_buy_signals(sigs, state, args.dry_run, budget, held)
        na = check_arrivals(sigs, state, args.dry_run, budget - nb, held)
    _record_sent(state, nb + na)
    ns = check_sell_alerts(holdings, state, args.dry_run)
    # 예산 소진 첫 순간 1회만 안내(이후 제안은 웹에서) — 조용한 누락 방지
    if (_sent_today(state) >= cfg.DAILY_ALERT_BUDGET
            and state.get("budget_notice_day") != cfg.today_kst()):
        state["budget_notice_day"] = cfg.today_kst()
        if not args.dry_run:
            notify.send(f"ℹ️ 오늘 제안 알림 {cfg.DAILY_ALERT_BUDGET}건 도달 — "
                        f"이후 후보는 웹에서 확인하세요. (폭주 방지 상한)")
    print(f"시그널 {len(sigs)}개 · 보유 {len(holdings)}종목 → "
          f"매수제안 {nb} · 도달알림 {na} · 매도제안 {ns}건 전송 "
          f"(오늘 누적 {_sent_today(state)}/{cfg.DAILY_ALERT_BUDGET})")
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
