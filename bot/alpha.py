"""성과 vs 지수(알파) 추적 — 계좌를 하나의 ETF처럼 벤치마크와 비교.

사용자 요청(2026-07-24): -1%가 잘한 건지 못한 건지는 지수를 봐야 안다.
  · 미장 세션 = 나스닥(^IXIC), 국장 세션 = 코스피(^KS11)·코스닥(^KQ11)과 비교.
  · 층위: 전체 계좌 / 전략별(A=전환확정·B=매물대) / (종목별은 조회 명령으로).
  · 알림: 장 시작(기준 설정)·1시간 간격·장 마감(요약+캡처 통계) + 꺾은선 그래프.
  · 캡처 통계: 지수 상승일에 우리가 얼마나(상승 캡처), 하락일에 얼마나 덜(하락 캡처)
    — "하락할 때 덜 떨어지고 상승할 때 더 오른다"의 표준 수치화. 일별 기록이
    쌓여야 의미(≥5일부터 표시).

측정 방식(플로우 중립·정직한 한계):
  · 세션 중 계좌% = (Σ평가손익 − 세션시작 Σ평가손익) / Σ매입금액 — 장중 신규
    매수는 손익 0으로 들어와 왜곡 없음. 지수%도 세션 시작가 대비(동일 기준).
  · 누적 = Σ평가손익(KRW 환산) / SEED 합 vs 최초 관측 시점 지수 대비 등락.
    (실현손익은 아직 미배선 — 매도 발생 시 오차 생김, costbook #25에서 개선.)
  · 사용자 기보유(baseline)는 집계에서 제외 — 봇 전략 성과만 잰다.

배선: 매수루프 _cycle()이 5분마다 tick() 호출(실패는 무해 — 매매에 영향 0).
지수 시세: 야후 v8 chart(무키·표준라이브러리). 실패 시 그 틱은 조용히 건너뜀.
"""
from __future__ import annotations

import datetime
import json
import os
import time
import urllib.parse
import urllib.request

from bot import kis, kis_positions, notify, settings

STATE_PATH = os.environ.get(
    "ALPHA_STATE_PATH", os.path.join(os.path.dirname(__file__), "alpha_state.json"))
ALERT_MIN = int(os.environ.get("ALPHA_ALERT_MIN", "60"))   # 중간 알림 간격(분)
SAMPLE_SECONDS = max(
    60, min(900, int(os.environ.get("ALPHA_SAMPLE_SECONDS", "300"))))
SERIES_MAX = 48                                            # 그래프 포인트 상한
IDX = {"US": [("^IXIC", "나스닥"), ("^GSPC", "S&P500")],
       "KR": [("^KS11", "코스피"), ("^KQ11", "코스닥")]}
_US_EXCGS = ("NASD", "NYSE", "AMEX")


# ── 데이터 수집 ────────────────────────────────────────────────
def _yahoo_last(sym: str) -> float | None:
    """지수 현재 레벨(야후 v8, 무키). 실패=None(틱 건너뜀)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(sym) + "?range=1d&interval=5m")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            m = json.load(r)["chart"]["result"][0]["meta"]
        v = m.get("regularMarketPrice")
        return float(v) if v else None
    except Exception:
        return None


def _broker_rows(market: str | None = None) -> list[dict] | None:
    """보유행. 파수꾼 공유 캐시 우선, 없을 때만 KIS 잔고를 직접 조회."""
    if market in ("KR", "US"):
        try:
            from bot import market_cache
            cached = market_cache.positions_for_market(
                market, max_age=90)
            if cached is not None:
                return cached
        except Exception:
            pass
    rows: dict[str, dict] = {}
    kr = kis.positions_detail("KR")
    if kr is None:
        return None
    for p in kr:
        rows.setdefault(p["code"], p)
    for ex in _US_EXCGS:
        us = kis.positions_detail("US", excg=ex)
        if us is None:
            return None
        for p in us:
            rows.setdefault(p["code"], p)
    return list(rows.values())


def aggregate(rows: list[dict], b_codes: set, baseline: set) -> dict:
    """시장×슬리브 집계 {mkt: {sleeve: {cost, pl}}} — baseline(기보유)은 제외."""
    out: dict = {"US": {"A": {"cost": 0.0, "pl": 0.0}, "B": {"cost": 0.0, "pl": 0.0}},
                 "KR": {"A": {"cost": 0.0, "pl": 0.0}, "B": {"cost": 0.0, "pl": 0.0}}}
    for p in rows:
        code = p["code"]
        if code in baseline:
            continue
        mkt = "KR" if p.get("market") == "KR" or p.get("ccy") == "KRW" else "US"
        sl = "B" if code in b_codes else "A"
        out[mkt][sl]["cost"] += float(p.get("buy_amt") or 0)
        out[mkt][sl]["pl"] += float(p.get("pl_amt") or 0)
    return out


# ── 상태 ──────────────────────────────────────────────────────
def _load() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(st: dict) -> None:
    st["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


# ── 계산 ──────────────────────────────────────────────────────
def _pct(pl_delta: float, cost: float) -> float:
    return (pl_delta / cost * 100.0) if cost > 0 else 0.0


def session_update(st: dict, mkt: str, agg: dict, idx: dict,
                   now_hhmm: str, today: str) -> dict:
    """세션 상태 갱신 → 계좌·A/B·모든 지수를 같은 0% 기준으로 저장."""
    day = st.setdefault("day", {}).get(mkt)
    tot_pl = agg[mkt]["A"]["pl"] + agg[mkt]["B"]["pl"]
    tot_cost = agg[mkt]["A"]["cost"] + agg[mkt]["B"]["cost"]
    if not day or day.get("date") != today:                 # 세션 첫 틱 = 기준
        day = {"date": today, "pl0": tot_pl,
               "a_pl0": agg[mkt]["A"]["pl"], "b_pl0": agg[mkt]["B"]["pl"],
               "idx0": {k: v for k, v in idx.items()}, "series": [],
               "series_v2": [],
               "opened": True, "closed": False}
        st["day"][mkt] = day
    day.setdefault("a_pl0", agg[mkt]["A"]["pl"])
    day.setdefault("b_pl0", agg[mkt]["B"]["pl"])
    day.setdefault("idx0", {k: v for k, v in idx.items()})
    day.setdefault("series", [])
    day.setdefault("series_v2", [])
    acct = _pct(tot_pl - day["pl0"], tot_cost)
    a = _pct(agg[mkt]["A"]["pl"] - day["a_pl0"], agg[mkt]["A"]["cost"])
    b = _pct(agg[mkt]["B"]["pl"] - day["b_pl0"], agg[mkt]["B"]["cost"])
    ipct = {}
    for name, v in idx.items():
        v0 = day["idx0"].get(name)
        if not v0:                              # 배포 중 새 지수 추가 시 그 틱을 0% 기준
            day["idx0"][name] = v
            v0 = v
        ipct[name] = (v / v0 - 1) * 100.0 if v0 else 0.0
    day["series"].append([now_hhmm, round(acct, 3),
                          round(next(iter(ipct.values()), 0.0), 3)])
    point = {
        "t": now_hhmm,
        "account": round(acct, 4),
        "A": round(a, 4),
        "B": round(b, 4),
        "indices": {name: round(value, 4) for name, value in ipct.items()},
    }
    if day["series_v2"] and day["series_v2"][-1].get("t") == now_hhmm:
        day["series_v2"][-1] = point
    else:
        day["series_v2"].append(point)
    if len(day["series"]) > SERIES_MAX * 2:                  # 상한 초과 시 솎아냄
        day["series"] = day["series"][::2]
    if len(day["series_v2"]) > SERIES_MAX * 4:
        day["series_v2"] = day["series_v2"][::2]
    return {"acct": acct, "idx": ipct, "a": a, "b": b,
            "series": day["series"], "series_v2": day["series_v2"]}


def capture_stats(days: list[dict], mkt: str) -> str:
    """상승/하락 캡처 + 지수 대비 승률. 표본<5면 빈 문자열."""
    rows = [d for d in days if d.get("mkt") == mkt]
    if len(rows) < 5:
        return ""
    up = [d for d in rows if d["idx"] > 0]
    dn = [d for d in rows if d["idx"] < 0]
    parts = [f"({len(rows)}일 기준)"]
    if up:
        cap = sum(d["acct"] for d in up) / sum(d["idx"] for d in up) * 100
        parts.append(f"상승일 캡처 {cap:.0f}%")
    if dn:
        cap = sum(d["acct"] for d in dn) / sum(d["idx"] for d in dn) * 100
        parts.append(f"하락일 캡처 {cap:.0f}% (낮을수록 방어 잘함)")
    win = sum(1 for d in rows if d["acct"] > d["idx"]) / len(rows) * 100
    parts.append(f"지수 이긴 날 {win:.0f}%")
    return " · ".join(parts)


def chart_url(series: list, idx_name: str, title: str) -> str:
    """세션 추이 꺾은선(QuickChart) — 계좌 vs 지수, 세션시작=0% 기준."""
    pts = series[-SERIES_MAX:]
    cfg = {"type": "line",
           "data": {"labels": [p[0] for p in pts],
                    "datasets": [
                        {"label": "내 계좌", "data": [p[1] for p in pts],
                         "fill": False, "borderColor": "#16a34a", "pointRadius": 0},
                        {"label": idx_name, "data": [p[2] for p in pts],
                         "fill": False, "borderColor": "#64748b", "pointRadius": 0}]},
           "options": {"title": {"display": True, "text": title}}}
    return ("https://quickchart.io/chart?w=560&h=320&c="
            + urllib.parse.quote(json.dumps(cfg, separators=(",", ":"))))


def publish_dash(st: dict) -> None:
    """대시보드용 컴팩트 상태를 ntfy 토픽에 발행 — 웹 perf.html이 조회.

    퍼센트만(금액·수량·계좌 없음). 4KB 한도 안: 세션 시리즈 40점·일별 30일."""
    try:
        day = {}
        for mkt, d in (st.get("day") or {}).items():
            if d.get("series"):
                s = d["series"]
                step = max(1, len(s) // 40)
                day[mkt] = {"date": d.get("date"), "series": s[::step][-40:]}
        payload = {"day": day, "days": (st.get("days") or [])[-30:]}
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            "https://ntfy.sh/" + settings.ALPHA_DASH_TOPIC, data=body,
            method="POST", headers={"Title": "alpha-dash", "Priority": "min",
                                    "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass                                       # 발행 실패 무해(다음 틱 재시도)


def dashboard_snapshot(st: dict | None = None) -> dict:
    """Oracle 개인 웹용 퍼센트 전용 스냅샷.

    계좌 금액·수량·종목은 내보내지 않는다. 구버전 상태도 주 지수 1개만이라도
    읽을 수 있게 변환해 배포 직후 화면이 완전히 비지 않도록 한다.
    """
    st = _load() if st is None else st
    markets = {}
    labels = {"US": "미국", "KR": "한국"}
    for market in ("US", "KR"):
        day = (st.get("day") or {}).get(market) or {}
        series = list(day.get("series_v2") or [])
        if not series:
            primary = IDX[market][0][1]
            series = [
                {"t": row[0], "account": float(row[1]), "A": None, "B": None,
                 "indices": {primary: float(row[2])}}
                for row in (day.get("series") or [])
                if isinstance(row, list) and len(row) >= 3
            ]
        markets[market] = {
            "label": labels[market],
            "date": day.get("date"),
            "indices": [name for _symbol, name in IDX[market]],
            "series": series[-SERIES_MAX * 4:],
        }
    days = []
    for row in (st.get("days") or [])[-120:]:
        market = row.get("mkt")
        if market not in ("US", "KR"):
            continue
        indices = dict(row.get("indices") or {})
        if not indices and row.get("idx") is not None:
            indices[IDX[market][0][1]] = float(row.get("idx") or 0)
        days.append({
            "date": row.get("d"),
            "market": market,
            "account": float(row.get("acct") or 0),
            "A": (float(row["a"]) if row.get("a") is not None else None),
            "B": (float(row["b"]) if row.get("b") is not None else None),
            "indices": {str(k): float(v) for k, v in indices.items()},
        })
    return {
        "version": 2,
        "generated_at": st.get("updated_at"),
        "sample_seconds": SAMPLE_SECONDS,
        "markets": markets,
        "days": days,
        "basis": "KIS 봇 보유 평가손익 기준",
    }


# ── 메인 틱 ────────────────────────────────────────────────────
def _fmt(v: float) -> str:
    return f"{v:+.2f}%"


def tick(now: datetime.datetime | None = None) -> None:
    """매수루프가 5분마다 호출. 세션 중 스냅샷·알림, 세션 종료 시 마감 요약."""
    now = now or datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9)))
    today = now.strftime("%Y-%m-%d")
    st = _load()
    for mkt, ccy in (("US", "USD"), ("KR", "KRW")):
        live = settings.market_open(ccy)
        day = (st.get("day") or {}).get(mkt)
        if not live:
            #  세션 방금 끝났으면 마감 요약 1회
            if day and day.get("date") == today and day.get("series") \
                    and not day.get("closed"):
                day["closed"] = True
                _close_alert(st, mkt, day)
                _save(st)
                publish_dash(st)               # 마감 요약도 대시보드에 반영
            continue
        sampled = float((st.get("sampled_at") or {}).get(mkt) or 0)
        if sampled and now.timestamp() - sampled < SAMPLE_SECONDS:
            continue                             # 5분 간격 유지(KIS·야후 호출 폭주 방지)
        rows = _broker_rows(mkt)
        if rows is None:
            continue                                   # 잔고 불명 — 이번 틱 건너뜀
        try:
            recs = kis_positions.load()
        except Exception:
            recs = {}
        b_codes = {c for c, i in recs.items() if i.get("sleeve") == "B"}
        try:
            from bot import ownership
            baseline = ownership.baseline() or set()
        except Exception:
            baseline = set()
        agg = aggregate(rows, b_codes, baseline)
        cost = agg[mkt]["A"]["cost"] + agg[mkt]["B"]["cost"]
        if cost <= 0:
            continue                                    # 이 시장 보유 없음 — 비교 무의미
        idx = {}
        for sym, name in IDX[mkt]:
            v = _yahoo_last(sym)
            if v:
                idx[name] = v
        if not idx:
            continue                                    # 지수 조회 실패 — 건너뜀
        r = session_update(st, mkt, agg, idx, now.strftime("%H:%M"), today)
        st.setdefault("sampled_at", {})[mkt] = now.timestamp()
        first = len(r["series"]) == 1
        last_alert = st.setdefault("alert", {}).get(mkt, 0)
        if first:
            st["alert"][mkt] = time.time()
            notify.send(f"📊 <b>성과 추적 시작</b> ({'미장' if mkt=='US' else '국장'})"
                        f" — 세션 기준점 설정. 1시간마다 지수 대비 비교 알림.")
        elif time.time() - last_alert >= ALERT_MIN * 60 - 90:
            st["alert"][mkt] = time.time()
            _mid_alert(st, mkt, r)
        _save(st)
        publish_dash(st)                       # 웹 대시보드(perf.html)용 발행


def _vs_line(acct: float, ipct: dict) -> str:
    main = next(iter(ipct.values()), 0.0)
    d = acct - main
    mark = "🟢" if d >= 0 else "🔴"
    idx_txt = " · ".join(f"{k} {_fmt(v)}" for k, v in ipct.items())
    return f"내 계좌 {_fmt(acct)} vs {idx_txt}\n→ 지수 대비 {mark} {d:+.2f}%p"


def _mid_alert(st: dict, mkt: str, r: dict) -> None:
    name = "미장·나스닥" if mkt == "US" else "국장·코스피/코스닥"
    body = (f"📊 <b>성과 vs 지수</b> ({name}, 장중)\n"
            + _vs_line(r["acct"], r["idx"])
            + f"\n전략별: A(전환) {_fmt(r['a'])} · B(매물대) {_fmt(r['b'])}")
    idx_name = next(iter(r["idx"].keys()), "지수")
    url = chart_url(r["series"], idx_name, f"오늘 장중 추이 vs {idx_name}")
    if not notify.send_photo(url, body):
        notify.send(body)


def _close_alert(st: dict, mkt: str, day: dict) -> None:
    if not day["series"]:
        return
    last = day["series"][-1]
    acct, ipct = last[1], last[2]
    rich = (day.get("series_v2") or [{}])[-1]
    d = acct - ipct
    days = st.setdefault("days", [])
    days.append({
        "d": day["date"], "mkt": mkt, "acct": acct, "idx": ipct,
        "a": rich.get("A"), "b": rich.get("B"),
        "indices": rich.get("indices") or {IDX[mkt][0][1]: ipct},
    })
    del days[:-120]                                      # 최근 120일만 보관
    cap = capture_stats(days, mkt)
    name = "미장·나스닥" if mkt == "US" else "국장·코스피"
    mark = "🟢" if d >= 0 else "🔴"
    body = (f"🏁 <b>장 마감 성과</b> ({name})\n"
            f"오늘: 내 계좌 {_fmt(acct)} vs 지수 {_fmt(ipct)} "
            f"→ {mark} {d:+.2f}%p\n"
            + (f"누적 통계: {cap}" if cap
               else "누적 통계: 5일 이상 쌓이면 상승/하락 캡처 표시"))
    idx_name = "나스닥" if mkt == "US" else "코스피"
    url = chart_url(day["series"], idx_name, f"{day['date']} 세션 추이")
    if not notify.send_photo(url, body):
        notify.send(body)
