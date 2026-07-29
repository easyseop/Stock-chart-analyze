"""수익 매도 뒤 주가를 추적하는 읽기 전용 분석.

확정 체결 거래이력과 별도 공개 일봉 캐시만 읽는다. KIS·주문·kill-switch를
import하거나 호출하지 않는다. 구버전 주문가로 복원한 매도도 개별 참고자료로는
표시하지만, 공통점 통계에는 브로커 체결가가 확인된 표본만 사용한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Callable, Iterable
from zoneinfo import ZoneInfo


HORIZONS = (1, 3, 5, 10, 20)
VERSION = 1
SNAPSHOT_PATH = os.environ.get(
    "POST_EXIT_SNAPSHOT_PATH",
    "/var/lib/stock-post-exit/snapshot.json",
)
_MARKET_ZONES = {
    "KR": ZoneInfo("Asia/Seoul"),
    "US": ZoneInfo("America/New_York"),
}


def _number(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _pct(value: float, base: float) -> float:
    return (float(value) / float(base) - 1.0) * 100.0


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _session_day(executed_at: object, market: str) -> str:
    """체결시각을 해당 거래소의 세션 날짜로 바꾼다.

    거래이력은 KST로 표시된다. 미국 장 마감 뒤 KST 날짜를 그대로 쓰면 다음
    미국 세션을 하루 건너뛰므로 반드시 뉴욕 날짜로 환산한다.
    """
    try:
        stamp = datetime.fromisoformat(str(executed_at))
    except (TypeError, ValueError):
        return ""
    if stamp.tzinfo is None:
        return ""
    zone = _MARKET_ZONES.get(str(market).upper())
    return stamp.astimezone(zone).date().isoformat() if zone else ""


def _event_id(row: dict) -> str:
    public = "|".join(str(row.get(key) or "") for key in (
        "code", "executed_at", "qty", "entry_price", "exit_price",
    ))
    return hashlib.sha256(public.encode("utf-8")).hexdigest()[:16]


def _bars_from_frame(frame) -> list[dict]:
    """scanner cache DataFrame을 표준 일봉 목록으로 바꾼다."""
    if frame is None:
        return []
    rows: list[dict] = []
    try:
        iterator = frame.iterrows()
    except AttributeError:
        return []
    for idx, row in iterator:
        try:
            date = idx.strftime("%Y-%m-%d")
        except AttributeError:
            date = str(idx)[:10]
        close = _number(row.get("Close"))
        if close is None or close <= 0:
            continue
        high = _number(row.get("High"))
        low = _number(row.get("Low"))
        high = high if high is not None and high > 0 else close
        low = low if low is not None and low > 0 else close
        rows.append({
            "date": date,
            "high": max(close, high),
            "low": min(close, low),
            "close": close,
        })
    return rows


def _cached_bars(code: str) -> list[dict]:
    """기존 일봉 캐시만 읽는다. HTTP 요청 경로에서 네트워크 갱신은 하지 않는다."""
    from scanner import cache
    return _bars_from_frame(cache.load(code))


def _clean_bars(rows: Iterable[dict]) -> list[dict]:
    clean: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        date = str(row.get("date") or "")[:10]
        close = _number(row.get("close") if "close" in row else row.get("Close"))
        high = _number(row.get("high") if "high" in row else row.get("High"))
        low = _number(row.get("low") if "low" in row else row.get("Low"))
        if len(date) != 10 or close is None or close <= 0:
            continue
        high = high if high is not None and high > 0 else close
        low = low if low is not None and low > 0 else close
        clean[date] = {
            "date": date,
            "close": close,
            "high": max(close, high),
            "low": min(close, low),
        }
    return [clean[key] for key in sorted(clean)]


def _profitable_sales(trades: Iterable[dict]) -> list[dict]:
    rows: list[dict] = []
    for trade in trades or []:
        if not isinstance(trade, dict) or str(trade.get("side")).lower() != "sell":
            continue
        entry = _number(trade.get("entry_price"))
        exit_price = _number(trade.get("exit_price"))
        reported = _number(trade.get("return_pct"))
        if entry is None or exit_price is None or entry <= 0 or exit_price <= entry:
            continue
        if reported is not None and reported <= 0:
            continue
        market = "KR" if str(trade.get("market")).upper() == "KR" else "US"
        session_day = _session_day(trade.get("executed_at"), market)
        if not session_day:
            continue
        rows.append({
            **trade,
            "market": market,
            "_entry": entry,
            "_exit": exit_price,
            "_session_day": session_day,
        })
    rows.sort(key=lambda row: str(row.get("executed_at") or ""), reverse=True)
    return rows


def _observation(trade: dict, bars: list[dict], horizon: int) -> dict | None:
    future = [bar for bar in bars if bar["date"] > trade["_session_day"]]
    if not future:
        return None
    window = future[:horizon]
    entry, exit_price = trade["_entry"], trade["_exit"]
    peak = max(float(row["high"]) for row in window)
    trough = min(float(row["low"]) for row in window)
    close = float(window[-1]["close"])
    return {
        "horizon": horizon,
        "observed_sessions": len(window),
        "complete": len(future) >= horizon,
        "through_date": window[-1]["date"],
        "close_price": _round(close, 6),
        "peak_price": _round(peak, 6),
        "close_vs_entry_pct": _round(_pct(close, entry)),
        "peak_vs_entry_pct": _round(_pct(peak, entry)),
        # 원 평단을 분모로 한 수익률에서 익절 뒤 더 붙은 %p.
        "additional_entry_points_after_exit": _round(
            (peak - exit_price) / entry * 100.0),
        # 매도대금을 계속 보유했다고 가정했을 때 놓친 상승률.
        "missed_upside_vs_exit_pct": _round(_pct(peak, exit_price)),
        "close_vs_exit_pct": _round(_pct(close, exit_price)),
        # 매도가보다 계속 위에 있었다면 불리한 움직임은 0%다.
        "max_drawdown_vs_exit_pct": _round(min(0.0, _pct(trough, exit_price))),
    }


def _trait_key(event: dict, kind: str) -> tuple[str, str]:
    if kind == "sleeve":
        value = "B" if event.get("sleeve") == "B" else "A"
        return f"sleeve:{value}", f"전략 {value}"
    if kind == "exit_type":
        partial = bool(event.get("partial_exit"))
        return ("exit:partial", "부분익절") if partial else ("exit:full", "전량익절")
    if kind == "return_band":
        value = float(event.get("exit_return_pct") or 0)
        if value < 5:
            return "return:0-5", "익절수익 0~5%"
        if value < 10:
            return "return:5-10", "익절수익 5~10%"
        return "return:10+", "익절수익 10%+"
    reason = str(event.get("reason_kind") or "other")
    labels = {
        "take_profit": "목표·+1R 익절",
        "trail": "트레일 청산",
        "time_stop": "기간 청산",
        "other": "기타 수익청산",
    }
    return f"reason:{reason}", labels.get(reason, reason)


def _traits(events: list[dict], horizon: int) -> list[dict]:
    """확정 체결·완료 관측만 그룹화한다. 표본 3건 미만은 결론 불가."""
    groups: dict[str, dict] = {}
    for event in events:
        if event.get("quality") != "verified":
            continue
        obs = (event.get("observations") or {}).get(str(horizon))
        if not obs or obs.get("complete") is not True:
            continue
        for kind in ("sleeve", "reason", "exit_type", "return_band"):
            key, label = _trait_key(event, kind)
            bucket = groups.setdefault(key, {
                "key": key, "label": label, "kind": kind, "values": [],
            })
            bucket["values"].append(obs)
    out: list[dict] = []
    for bucket in groups.values():
        values = bucket.pop("values")
        sample = len(values)
        out.append({
            **bucket,
            "horizon": horizon,
            "sample": sample,
            "conclusion_ready": sample >= 3,
            "avg_additional_entry_points": _round(sum(
                float(row["additional_entry_points_after_exit"])
                for row in values) / sample),
            "avg_missed_upside_vs_exit_pct": _round(sum(
                float(row["missed_upside_vs_exit_pct"])
                for row in values) / sample),
            "continued_higher_close_rate": _round(sum(
                float(row["close_vs_exit_pct"]) > 0 for row in values)
                / sample * 100.0, 2),
            "extended_5pct_rate": _round(sum(
                float(row["missed_upside_vs_exit_pct"]) >= 5.0
                for row in values) / sample * 100.0, 2),
        })
    return sorted(
        out,
        key=lambda row: (
            not row["conclusion_ready"],
            -row["sample"],
            -float(row["avg_missed_upside_vs_exit_pct"]),
        ),
    )


def build_snapshot(
        trades: Iterable[dict],
        bars_by_code: dict[str, Iterable[dict]],
        *,
        history_partial: bool = False,
        generated_at: str | None = None,
        limit: int = 100,
) -> dict:
    """주입된 체결과 일봉으로 API payload를 만든다. 네트워크·파일 쓰기 0건."""
    events: list[dict] = []
    sales = _profitable_sales(trades)
    for sale in sales[:max(1, min(500, int(limit)))]:
        code = str(sale.get("code") or "").upper()
        entry, exit_price = sale["_entry"], sale["_exit"]
        bars = _clean_bars(bars_by_code.get(code) or [])
        observations = {
            str(horizon): value
            for horizon in HORIZONS
            if (value := _observation(sale, bars, horizon)) is not None
        }
        events.append({
            "id": _event_id(sale),
            "executed_at": str(sale.get("executed_at") or ""),
            "session_day": sale["_session_day"],
            "code": code,
            "name": str(sale.get("name") or code),
            "market": sale["market"],
            "ccy": "KRW" if sale["market"] == "KR" else "USD",
            "sleeve": "B" if sale.get("sleeve") == "B" else "A",
            "reason": str(sale.get("reason") or "수익 매도"),
            "reason_kind": str(sale.get("reason_kind") or "other"),
            "qty": int(_number(sale.get("qty")) or 0),
            "entry_price": _round(entry, 6),
            "exit_price": _round(exit_price, 6),
            "exit_return_pct": _round(
                _number(sale.get("return_pct")) or _pct(exit_price, entry)),
            "partial_exit": bool(sale.get("partial_exit")),
            "quality": "verified" if sale.get("verified") is True else "estimated",
            "price_source": str(sale.get("fill_price_source") or "unknown"),
            "observations": observations,
        })

    def completed(horizon: int, *, verified_only: bool = True) -> list[dict]:
        return [
            event for event in events
            if (not verified_only or event["quality"] == "verified")
            and (event["observations"].get(str(horizon)) or {}).get("complete") is True
        ]

    five = completed(5)
    five_values = [
        event["observations"]["5"] for event in five
    ]
    verified = sum(event["quality"] == "verified" for event in events)
    estimated = len(events) - verified
    return {
        "version": VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "available": True,
        "partial": bool(history_partial),
        "source": "confirmed-local-fill-journals+public-daily-cache",
        "horizons": list(HORIZONS),
        "summary": {
            "profitable_exits": len(events),
            "verified_exits": verified,
            "estimated_exits": estimated,
            "tracked_exits": sum(bool(event["observations"]) for event in events),
            "complete_5d_verified": len(five),
            "avg_additional_entry_points_5d": _round(
                sum(float(row["additional_entry_points_after_exit"])
                    for row in five_values) / len(five_values)
                if five_values else None),
            "avg_missed_upside_vs_exit_5d": _round(
                sum(float(row["missed_upside_vs_exit_pct"])
                    for row in five_values) / len(five_values)
                if five_values else None),
            "continued_higher_close_5d_rate": _round(
                sum(float(row["close_vs_exit_pct"]) > 0 for row in five_values)
                / len(five_values) * 100.0 if five_values else None, 2),
        },
        "traits": {
            str(horizon): _traits(events, horizon)
            for horizon in (5, 20)
        },
        "events": events,
        "message": (
            "구버전 주문가 기반 매도는 참고표시만 하며 공통점 통계에서 제외합니다. "
            "브로커 체결가가 확인된 표본이 3건 이상인 묶음부터 공통점을 판정합니다."
        ),
    }


def snapshot(
        limit: int = 100,
        *,
        history: dict | None = None,
        loader: Callable[[str], Iterable[dict]] | None = None,
) -> dict:
    """현재 원장과 기존 일봉 캐시로 계산한다. 캐시 갱신·KIS 호출은 0건."""
    if history is None:
        from bot import trade_history
        history = trade_history.snapshot(limit=500)
    if history.get("available") is False:
        return {
            "version": VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "available": False,
            "partial": True,
            "source": "confirmed-local-fill-journals+public-daily-cache",
            "horizons": list(HORIZONS),
            "summary": {},
            "traits": {"5": [], "20": []},
            "events": [],
            "message": "거래 원장 무결성 확인 전에는 익절 사후추적을 표시하지 않습니다.",
        }
    sales = _profitable_sales(history.get("trades") or [])
    load = loader or _cached_bars
    codes = sorted({str(row.get("code") or "").upper() for row in sales})
    bars = {code: load(code) for code in codes}
    return build_snapshot(
        history.get("trades") or [],
        bars,
        history_partial=bool(history.get("partial")),
        limit=limit,
    )


def _atomic_write(payload: dict, path: str = SNAPSHOT_PATH) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"))
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, target)
        os.chmod(target, 0o640)
        directory = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def refresh_published(limit: int = 100) -> dict:
    """공개 일봉을 갱신한 뒤 안전한 분석 JSON을 원자 발행한다.

    이 함수만 네트워크·캐시 쓰기를 허용하며 별도 낮은 우선순위 timer에서 실행한다.
    """
    from bot import trade_history
    from scanner import cache

    history = trade_history.snapshot(limit=500)
    if history.get("available") is False:
        payload = snapshot(limit=limit, history=history)
        _atomic_write(payload)
        return payload
    sales = _profitable_sales(history.get("trades") or [])
    bars: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for code in sorted({str(row.get("code") or "").upper() for row in sales}):
        try:
            bars[code] = _bars_from_frame(cache.update(code))
        except Exception as exc:
            bars[code] = _bars_from_frame(cache.load(code))
            errors[code] = type(exc).__name__
    payload = build_snapshot(
        history.get("trades") or [],
        bars,
        history_partial=bool(history.get("partial") or errors),
        limit=limit,
    )
    payload["refresh"] = {
        "symbols": len(bars),
        "failed_symbols": sorted(errors),
    }
    _atomic_write(payload)
    return payload


def read_published(path: str = SNAPSHOT_PATH) -> dict:
    """웹용 원자 스냅샷. 없거나 손상되면 추측하지 않고 unavailable."""
    try:
        with open(path, encoding="utf-8") as fp:
            payload = json.load(fp)
        if (not isinstance(payload, dict)
                or payload.get("version") != VERSION
                or payload.get("read_only") is not True
                or not isinstance(payload.get("events"), list)):
            raise ValueError("invalid post-exit snapshot")
        return payload
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return {
            "version": VERSION,
            "generated_at": None,
            "read_only": True,
            "available": False,
            "partial": True,
            "source": "confirmed-local-fill-journals+public-daily-cache",
            "horizons": list(HORIZONS),
            "summary": {},
            "traits": {"5": [], "20": []},
            "events": [],
            "message": "첫 일봉 갱신 뒤 익절 사후추적이 표시됩니다.",
        }
