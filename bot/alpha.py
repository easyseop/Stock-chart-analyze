"""성과 vs 지수(알파) 추적 — 계좌를 하나의 ETF처럼 벤치마크와 비교.

사용자 요청(2026-07-24): -1%가 잘한 건지 못한 건지는 지수를 봐야 안다.
  · 미장 세션 = 나스닥(^IXIC), 국장 세션 = 코스피(^KS11)·코스닥(^KQ11)과 비교.
  · 층위: 전체 계좌 / 전략별(A=전환확정·B=매물대) / (종목별은 조회 명령으로).
  · 알림: 장 시작(기준 설정)·1시간 간격·장 마감(요약+캡처 통계) + 꺾은선 그래프.
  · 캡처 통계: 지수 상승일에 우리가 얼마나(상승 캡처), 하락일에 얼마나 덜(하락 캡처)
    — "하락할 때 덜 떨어지고 상승할 때 더 오른다"의 표준 수치화. 일별 기록이
    쌓여야 의미(≥5일부터 표시).

측정 방식(플로우 중립):
  · KIS 봇 보유 평가액을 NAV로 보고, costbook의 매수·매도 현금흐름을 각 관측
    구간에서 제거한 TWR을 누적한다. 신규매수·부분매도·시드 크기가 수익률을
    희석하거나 부풀리지 않고 가격 변화와 실현손익만 남는다.
  · 미국 평가는 환율을 포함한다. 두 지수는 전일 종가를 0%로 맞추며, 첫 배포일처럼
    전일 carry가 없을 때만 첫 관측값 기준이라고 명시한다.
  · 종목 선택 품질은 계좌 TWR과 섞지 않고 장 시작 보유종목의 전일종가 대비
    동일가중 평균을 별도 지표로 제공한다.
  · 사용자 기보유(baseline)는 집계에서 제외 — 봇 전략 성과만 잰다.

배선: 매수루프 _cycle()이 5분마다 tick() 호출(실패는 무해 — 매매에 영향 0).
지수 시세: 야후 v8 chart(무키·표준라이브러리). 실패 시 그 틱은 조용히 건너뜀.
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from bot import costbook, kis, kis_positions, ledger, notify, settings

STATE_PATH = os.environ.get(
    "ALPHA_STATE_PATH", os.path.join(os.path.dirname(__file__), "alpha_state.json"))
ALERT_MIN = int(os.environ.get("ALPHA_ALERT_MIN", "60"))   # 중간 알림 간격(분)
SAMPLE_SECONDS = max(
    60, min(900, int(os.environ.get("ALPHA_SAMPLE_SECONDS", "300"))))
SERIES_MAX = 48                                            # 그래프 포인트 상한
SNAPSHOT_VERSION = 4
IDX = {"US": [("^IXIC", "나스닥"), ("^GSPC", "S&P500")],
       "KR": [("^KS11", "코스피"), ("^KQ11", "코스닥")]}
_US_EXCGS = ("NASD", "NYSE", "AMEX")


# ── 데이터 수집 ────────────────────────────────────────────────
def _yahoo_quote(sym: str) -> dict | None:
    """지수 현재 레벨+전일 종가(야후 v8). 실패=None."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(sym) + "?range=1d&interval=5m")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            m = json.load(r)["chart"]["result"][0]["meta"]
        current = m.get("regularMarketPrice")
        previous = (m.get("regularMarketPreviousClose")
                    or m.get("chartPreviousClose"))
        if not current:
            return None
        return {"current": float(current),
                "previous_close": float(previous) if previous else None}
    except Exception:
        return None


def _yahoo_last(sym: str) -> float | None:
    """구버전 호출 호환용 현재값."""
    quote = _yahoo_quote(sym)
    return quote["current"] if quote else None


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
    parent = os.path.dirname(os.path.abspath(STATE_PATH)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".alpha-state-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as f:
            fd = -1
            json.dump(st, f, ensure_ascii=False, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
        os.chmod(STATE_PATH, 0o600)
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def rebase_after_accounting_migration(
    epoch_id: str,
    *,
    started_at: float | datetime.datetime | None = None,
    archived: bool = False,
) -> dict:
    """레거시 회계 이관 완료 뒤 성과와 지수를 같은 새 0% 기준으로 시작한다.

    같은 plan SHA 재실행은 이미 쌓인 새 성과를 다시 지우지 않는다. 손상된 과거
    일별·장중 수치는 운영 화면에서 제외하고 이관 도구의 forensic 백업에만 남긴다.
    """
    identity = str(epoch_id or "").strip()
    if not identity:
        raise ValueError("performance epoch_id 필요")
    current = _load()
    epoch = current.get("performance_epoch") or {}
    if epoch.get("id") == identity:
        return {
            "ok": True, "rebased": False, "already_applied": True,
            "epoch_id": identity,
        }
    if isinstance(started_at, datetime.datetime):
        stamp = started_at
    elif started_at is None:
        stamp = datetime.datetime.now(datetime.timezone.utc)
    else:
        stamp = datetime.datetime.fromtimestamp(
            float(started_at), tz=datetime.timezone.utc)
    payload = {
        "schema_version": SNAPSHOT_VERSION,
        "performance_epoch": {
            "id": identity,
            "started_at": stamp.isoformat(),
            "label": "장부 이관 후 새 기준",
            "basis": "account_and_indices_same_first_sample",
            "archived_previous_state": bool(archived),
        },
        "day": {},
        "days": [],
        "carry": {},
        "sampled_at": {},
        "alert": {},
    }
    _save(payload)
    # 공개 perf 캐시에도 빈 epoch를 즉시 발행해 이전 -17%가 다음 5분 틱까지
    # 남지 않게 한다. publish_dash는 네트워크 실패를 자체적으로 흡수한다.
    publish_dash(payload)
    return {
        "ok": True, "rebased": True, "already_applied": False,
        "epoch_id": identity,
    }


# ── 계산 ──────────────────────────────────────────────────────
def _pct(pl_delta: float, cost: float) -> float:
    return (pl_delta / cost * 100.0) if cost > 0 else 0.0


def nav_inputs(agg: dict, mkt: str) -> dict:
    """보유 평가액과 누적 매매 현금흐름으로 시장×전략 NAV 입력을 만든다.

    보유만 보는 화면에서 매수는 +평가액/+외부흐름, 매도는 -평가액/-외부흐름으로
    상쇄된다. 따라서 종목 교체나 시드 크기가 수익률을 희석하지 않고 가격·실현손익
    변화만 TWR에 남는다.
    """
    fx = 1.0 if mkt == "KR" else float(settings.FX_USDKRW)
    out = {}
    for sleeve in ("A", "B"):
        bucket = agg[mkt][sleeve]
        flows = costbook.market_totals(mkt, sleeve)
        out[sleeve] = {
            "value": (float(bucket["cost"]) + float(bucket["pl"])) * fx,
            "flow": float(flows["buy_cost"]) - float(flows["sell_proceeds"]),
        }
    out["account"] = {
        "value": out["A"]["value"] + out["B"]["value"],
        "flow": out["A"]["flow"] + out["B"]["flow"],
    }
    return out


def holdings_equal_weight(rows: list[dict], mkt: str, recs: dict,
                          baseline: set, today: str,
                          start_codes: set[str] | None = None) -> dict:
    """장 시작 전부터 보유한 종목의 전일종가 대비 수익률을 동일가중 평균한다.

    스캐너가 이미 가진 일봉 캐시만 읽고 네트워크를 추가 호출하지 않는다. 오늘
    신규매수는 장 시작 보유가 아니므로 제외한다. 캐시가 없는 종목은 coverage에
    반영하고 추측하지 않는다.
    """
    from scanner import cache
    by_sleeve = {"A": [], "B": []}
    eligible = 0
    for row in rows:
        code = str(row.get("code") or "").upper()
        market = ("KR" if row.get("market") == "KR" or row.get("ccy") == "KRW"
                  else "US")
        if not code or market != mkt or code in baseline:
            continue
        if start_codes is not None and code not in start_codes:
            continue                              # 장중 신규·수동 매수도 섞지 않음
        rec = recs.get(code) or {}
        opened = str(rec.get("opened") or "")
        if not opened or opened >= today:
            continue                              # 추적일 불명·오늘 신규는 장시작 보유 아님
        eligible += 1
        frame = cache.load(code)                  # 캐시만: 없다고 신규 수집 금지
        if frame is None or frame.empty:
            continue
        try:
            prior = frame[frame.index.strftime("%Y-%m-%d") < today]
            prev = float(prior["Close"].iloc[-1])
            cur = float(row.get("cur") or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if prev <= 0 or cur <= 0:
            continue
        sleeve = "B" if str(rec.get("sleeve") or "A").upper() == "B" else "A"
        by_sleeve[sleeve].append((cur / prev - 1.0) * 100.0)
    all_values = by_sleeve["A"] + by_sleeve["B"]
    avg = lambda values: (sum(values) / len(values)) if values else None
    return {
        "account": avg(all_values), "A": avg(by_sleeve["A"]),
        "B": avg(by_sleeve["B"]), "covered": len(all_values),
        "eligible": eligible,
    }


def _twr_step(day: dict, nav: dict, *, settled: bool = True) -> dict[str, float]:
    """한 관측 구간의 외부흐름을 제거해 일중 TWR을 누적한다.

    ``settled=False``(미회계 체결 존재)면 **기준선을 그대로 두고** 누적을 건너뛴다.
    보유평가액(브로커)만 먼저 움직이고 현금흐름(원장)이 늦게 붙는 구간을 그대로
    누적하면 매도가 유령 손실로 각인되기 때문이다. 회계가 끝난 뒤 원래 기준선에서
    한 번에 계산하면 평가액 변화와 외부흐름이 같은 구간에서 상쇄돼 정확해진다.
    """
    values = {}
    prev = day.setdefault("nav_prev", {})
    wealth = day.setdefault("wealth", {"account": 1.0, "A": 1.0, "B": 1.0})
    if not settled:
        day["accounting_pending"] = True
        return {key: (float(wealth.get(key, 1.0)) - 1.0) * 100.0
                for key in ("account", "A", "B")}
    day["accounting_pending"] = False
    for key in ("account", "A", "B"):
        cur_value = float((nav.get(key) or {}).get("value") or 0)
        cur_flow = float((nav.get(key) or {}).get("flow") or 0)
        old = prev.get(key)
        if old and float(old.get("value") or 0) > 0:
            external = cur_flow - float(old.get("flow") or 0)
            interval = (cur_value - float(old["value"]) - external) / float(old["value"])
            # 데이터 손상/통화 급변으로 -100% 이하가 나오면 그 틱은 누적하지 않는다.
            if interval > -1 and abs(interval) < 5:
                wealth[key] = float(wealth.get(key, 1.0)) * (1.0 + interval)
        prev[key] = {"value": cur_value, "flow": cur_flow}
        values[key] = (float(wealth.get(key, 1.0)) - 1.0) * 100.0
    day["nav_last"] = {key: dict(value) for key, value in prev.items()}
    return values


def session_update(st: dict, mkt: str, agg: dict, idx: dict,
                   now_hhmm: str, today: str, *,
                   nav: dict | None = None,
                   idx_previous_close: dict | None = None,
                   holdings_daily: dict | None = None,
                   holding_start_codes: set[str] | None = None,
                   accounting_settled: bool = True) -> dict:
    """세션 상태 갱신 → 계좌·A/B·모든 지수를 같은 0% 기준으로 저장."""
    day = st.setdefault("day", {}).get(mkt)
    tot_pl = agg[mkt]["A"]["pl"] + agg[mkt]["B"]["pl"]
    tot_cost = agg[mkt]["A"]["cost"] + agg[mkt]["B"]["cost"]
    carry = (st.get("carry") or {}).get(mkt)
    nav_mode = nav is not None
    if (not day or day.get("date") != today
            or (nav_mode and day.get("calc_version") != 3)):
        use_carry = bool(carry and nav_mode)
        day = {"date": today, "pl0": tot_pl,
               "a_pl0": agg[mkt]["A"]["pl"], "b_pl0": agg[mkt]["B"]["pl"],
               "idx0": {
                   k: ((idx_previous_close or {}).get(k) if use_carry
                       and (idx_previous_close or {}).get(k) else v)
                   for k, v in idx.items()},
               "series": [],
               "series_v2": [],
               "opened": True, "closed": False,
               "calc_version": 3 if nav_mode else 2,
               "basis": ("previous_close" if use_carry else "first_sample"),
               "holding_start_codes": sorted(holding_start_codes or set())}
        if use_carry:
            day["nav_prev"] = carry.get("nav_last") or {}
            day["wealth"] = {"account": 1.0, "A": 1.0, "B": 1.0}
        st["day"][mkt] = day
    day.setdefault("a_pl0", agg[mkt]["A"]["pl"])
    day.setdefault("b_pl0", agg[mkt]["B"]["pl"])
    day.setdefault("idx0", {k: v for k, v in idx.items()})
    day.setdefault("series", [])
    day.setdefault("series_v2", [])
    day.setdefault("holding_start_codes", sorted(holding_start_codes or set()))
    if nav_mode:
        twr = _twr_step(day, nav, settled=accounting_settled)
        acct, a, b = twr["account"], twr["A"], twr["B"]
    else:
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
    if day.get("basis") == "first_sample":
        # 리베이스 첫날 계좌 TWR과 동일한 첫 관측값을 지수의 일간 기준으로 쓴다.
        # 전일종가 갭을 지수에만 넣으면 1m/3m/전체에 영구 복리되는 비교 오차가 난다.
        daily_indices = dict(ipct)
    else:
        daily_indices = {
            name: (value / (idx_previous_close or {}).get(name) - 1) * 100
            for name, value in idx.items()
            if (idx_previous_close or {}).get(name)
        }
    point = {
        "t": now_hhmm,
        "account": round(acct, 4),
        "A": round(a, 4),
        "B": round(b, 4),
        "indices": {name: round(value, 4) for name, value in ipct.items()},
        "holdings": {
            key: (round(value, 4) if value is not None else None)
            for key, value in (holdings_daily or {}).items()
        },
        "daily_indices": {
            name: round(value, 4) for name, value in daily_indices.items()
        },
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
            "basis": day.get("basis") or "first_sample",
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
            "basis": row.get("basis") or "previous_close",
            "account": float(row.get("acct") or 0),
            "A": (float(row["a"]) if row.get("a") is not None else None),
            "B": (float(row["b"]) if row.get("b") is not None else None),
            "indices": {str(k): float(v) for k, v in indices.items()},
            "holdings": dict(row.get("holdings") or {}),
            "daily_indices": dict(row.get("daily_indices") or {}),
        })
    epoch = st.get("performance_epoch") or {}
    return {
        "version": SNAPSHOT_VERSION,
        "generated_at": st.get("updated_at"),
        "sample_seconds": SAMPLE_SECONDS,
        "markets": markets,
        "days": days,
        "epoch": {
            "started_at": epoch.get("started_at"),
            "label": epoch.get("label"),
            "basis": epoch.get("basis"),
        } if epoch else None,
        "basis": "KIS 봇 운용자산 NAV/TWR · 매매 현금흐름 제거 · 미국은 환율 포함",
    }


# ── 메인 틱 ────────────────────────────────────────────────────
def _fmt(v: float) -> str:
    return f"{v:+.2f}%"


def tick(now: datetime.datetime | None = None) -> None:
    """매수루프가 5분마다 호출. 세션 중 스냅샷·알림, 세션 종료 시 마감 요약."""
    now = now or datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9)))
    st = _load()
    for mkt, ccy in (("US", "USD"), ("KR", "KRW")):
        session_day = (now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                       if mkt == "US" else now.date().isoformat())
        live = settings.market_open(ccy)
        day = (st.get("day") or {}).get(mkt)
        if not live:
            #  세션 방금 끝났으면 마감 요약 1회
            if day and day.get("date") == session_day and day.get("series") \
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
        idx_previous = {}
        for sym, name in IDX[mkt]:
            quote = _yahoo_quote(sym)
            if quote:
                idx[name] = quote["current"]
                if quote.get("previous_close"):
                    idx_previous[name] = quote["previous_close"]
        if not idx:
            continue                                    # 지수 조회 실패 — 건너뜀
        existing_day = (st.get("day") or {}).get(mkt) or {}
        persisted_start = (
            existing_day.get("holding_start_codes")
            if existing_day.get("date") == session_day else None)
        if persisted_start is None:
            start_codes = set()
            for row in rows:
                code = str(row.get("code") or "").upper()
                row_market = (
                    "KR" if row.get("market") == "KR" or row.get("ccy") == "KRW"
                    else "US")
                if not code or row_market != mkt or code in baseline:
                    continue
                opened = str((recs.get(code) or {}).get("opened") or "")
                if not opened or opened >= session_day:
                    continue
                start_codes.add(code)
        else:
            start_codes = {str(code).upper() for code in persisted_start}
        try:
            settled = ledger.unaccounted_fills() == 0
        except Exception:
            settled = False                  # 원장 판정 불가 = 누적 보류(fail-closed)
        r = session_update(
            st, mkt, agg, idx, now.strftime("%H:%M"), session_day,
            nav=nav_inputs(agg, mkt), idx_previous_close=idx_previous,
            holdings_daily=holdings_equal_weight(
                rows, mkt, recs, baseline, session_day,
                start_codes=start_codes),
            holding_start_codes=start_codes,
            accounting_settled=settled)
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
    # 회계 미정산으로 마감한 날의 계좌 수익률은 매도 시차만 반영된 값일 수 있다.
    #   그 값을 누적 기록에 넣으면 1개월·3개월·전체가 영구 오염된다(예전 -17% 사례).
    #   기록은 보류하고 다음 정산 세션부터 이어간다(지수·carry는 정상 저장).
    pending = bool(day.get("accounting_pending"))
    if not pending:
        days.append({
            "d": day["date"], "mkt": mkt, "acct": acct, "idx": ipct,
            "basis": day.get("basis") or "first_sample",
            "a": rich.get("A"), "b": rich.get("B"),
            "indices": rich.get("indices") or {IDX[mkt][0][1]: ipct},
            "holdings": rich.get("holdings") or {},
            "daily_indices": rich.get("daily_indices") or {},
        })
    if day.get("nav_last"):
        st.setdefault("carry", {})[mkt] = {
            "date": day.get("date"), "nav_last": day["nav_last"]}
    del days[:-120]                                      # 최근 120일만 보관
    cap = capture_stats(days, mkt)
    name = "미장·나스닥" if mkt == "US" else "국장·코스피"
    mark = "🟢" if d >= 0 else "🔴"
    if pending:
        body = (f"🏁 <b>장 마감 성과</b> ({name})\n"
                f"⏳ 매도 회계 정산 대기 — 계좌 수익률 산정 보류"
                f"(지수 {_fmt(ipct)})\n"
                "정산되면 다음 세션부터 이어집니다. 누적 기록에는 넣지 않았습니다.")
    else:
        body = (f"🏁 <b>장 마감 성과</b> ({name})\n"
                f"오늘: 내 계좌 {_fmt(acct)} vs 지수 {_fmt(ipct)} "
                f"→ {mark} {d:+.2f}%p\n"
                + (f"누적 통계: {cap}" if cap
                   else "누적 통계: 5일 이상 쌓이면 상승/하락 캡처 표시"))
    idx_name = "나스닥" if mkt == "US" else "코스피"
    url = chart_url(day["series"], idx_name, f"{day['date']} 세션 추이")
    if not notify.send_photo(url, body):
        notify.send(body)
