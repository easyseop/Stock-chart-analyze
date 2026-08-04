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

import contextlib
import datetime
import fcntl
import json
import math
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
@contextlib.contextmanager
def state_lock(timeout_s: float = 10.0):
    """alpha 상태 파일 쓰기 임계구역 — tick()과 유지보수 스크립트 공용.

    두 프로세스가 읽기-수정-쓰기를 겹치면 나중에 저장하는 쪽이 상대의 갱신을
    통째로 덮어쓴다(Codex P2-2: 재기준 스크립트가 동시 KR 틱을 유실시킴).

    무한 블로킹 금지: alpha는 buyloop 끝에서 동기 호출되므로 누가 잠금을 쥔 채
    멈추면 **다음 매수 사이클 전체가 멈춘다**(Codex TWR-V2 P2-2). timeout 안에
    못 잡으면 TimeoutError — 호출부는 alpha만 건너뛰고 매수는 계속한다.
    """
    path = STATE_PATH + ".lock"
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + max(0.1, float(timeout_s))
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"alpha 상태 잠금 {timeout_s}s 초과 — 성과 틱 건너뜀")
                time.sleep(0.2)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


QUALITY_PATH = os.environ.get(
    "ALPHA_QUALITY_PATH",
    os.path.join(os.path.dirname(__file__), "alpha_quality.jsonl"))

# 일별 마감 행의 append-only 장기 원장. state의 days[]는 시장별 400행 창이라
#   약 1.6년 뒤 앞부분이 잘리지만(Codex V4 P2-2), 사용자의 요구는 하루하루
#   장기 누적이다 — 마감마다 여기에 먼저 영속시켜 창 밖으로 밀려나도 원본이
#   남는다. 소비자는 아직 state 창을 읽으므로 UI는 보관 한도를 명시한다.
DAYS_LEDGER_PATH = os.environ.get(
    "ALPHA_DAYS_LEDGER_PATH",
    os.path.join(os.path.dirname(__file__), "alpha_days.jsonl"))
DAYS_RETENTION = 400                     # state 창(시장별) — UI 라벨과 정합 유지


_quality_fail_alerted = False


def _quality_append(ev: dict) -> None:
    """append-only 품질 원장(로컬 전용) — 격리·미확정 마감의 진단 근거 보존.

    day 내부 anomaly_log는 다음 세션 교체 때 사라진다(Codex P2-3). 다음 절벽의
    원인 확정에 필요한 원본을 여기 남긴다. 공개 API로는 내보내지 않는다.
    fsync로 전원 장애 내구성을 확보하고, 쓰기 실패는 1회 경보한다(조용한 소실
    방지 — Codex V3 P2-3).
    """
    global _quality_fail_alerted
    try:
        with open(QUALITY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **ev},
                               ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(QUALITY_PATH, 0o600)
        _quality_fail_alerted = False
    except Exception as exc:
        if not _quality_fail_alerted:
            _quality_fail_alerted = True
            try:
                notify.send(f"⚠️ 성과 품질 원장 기록 실패({type(exc).__name__})"
                            " — 진단 근거가 소실될 수 있음. 디스크·권한 확인 필요.",
                            category="trade")
            except Exception:
                pass


_days_ledger_fail_alerted = False


def _days_ledger_append(row: dict) -> None:
    """일별 마감 행을 append-only 장기 원장에 영속(P2-2). 실패는 1회 경보."""
    global _days_ledger_fail_alerted
    try:
        with open(DAYS_LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **row},
                               ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(DAYS_LEDGER_PATH, 0o600)
        _days_ledger_fail_alerted = False
    except Exception as exc:
        if not _days_ledger_fail_alerted:
            _days_ledger_fail_alerted = True
            try:
                notify.send(f"⚠️ 일별 성과 장기 원장 기록 실패({type(exc).__name__})"
                            " — 400일 창 밖 이력이 소실될 수 있음. 디스크 확인 필요.",
                            category="trade")
            except Exception:
                pass


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


def _interval_limit(first_of_session: bool) -> float:
    """한 관측 구간에서 정상으로 인정할 최대 변동폭.

    분산된 다종목 계좌가 5분 만에 몇 %씩 움직이는 일은 실질적으로 없다. 그런
    값이 보이면 시장이 아니라 **두 소스(브로커 평가액 · 원장 현금흐름)의 불일치**
    쪽을 먼저 의심해야 한다. 세션 첫 구간만 전일 종가 대비 갭을 포함하므로 넓다.
    """
    if first_of_session:
        return _abs_float(os.environ.get("ALPHA_MAX_GAP_MOVE"), 0.10)
    return _abs_float(os.environ.get("ALPHA_MAX_TICK_MOVE"), 0.03)


def _abs_float(raw: object, default: float) -> float:
    try:
        out = abs(float(raw))
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


# 이상 구간을 몇 틱까지 '지연으로 보고' 기준선을 붙들지. 그 뒤로도 계속되면
#   일시적 시차가 아니라 실제 상태 변화로 보고 기준선을 옮긴다(영구 동결 방지).
_ANOMALY_HOLD_TICKS = 3


_KEYS = ("account", "A", "B")


def _twr_step(day: dict, nav: dict, *, settled: bool = True,
              context: dict | None = None) -> dict[str, float | None]:
    """한 관측 구간의 외부흐름을 제거해 일중 TWR을 누적한다.

    이 계산은 보유평가액을 **브로커 실시간**에서, 현금흐름을 **로컬 원장**에서
    읽어 뺀다. 두 소스가 한 틱이라도 어긋나면 그 차이가 통째로 손익으로 둔갑하고,
    한 번 곱해진 값은 세션 내내 남아 마감 기록·장기 누적까지 오염된다(2026-07
    -17%, 08-03 -6.2%p, 08-04 -4.9%p).

    한계를 넘는 구간에서 **그것이 실제 손익인지 데이터 시차인지는 이 자리에서
    판정할 수 없다.** 그래서 어느 쪽으로도 확정하지 않는다.
      ① ``settled=False``(미회계 체결) — 알려진 시차. 기준선 유지·누적 보류.
      ② 한계 초과 — 해당 키만 보류하고 기준선을 붙든다(시차면 자동 상쇄).
      ③ 보류가 ``_ANOMALY_HOLD_TICKS``를 넘게 지속 — 시차가 아니다. 그렇다고
         실제 손익이라고 단정할 수도 없으므로 **그 키의 세션 값을 `None`(미확정)
         으로 격리**하고 이후 추적만 새 기준선에서 재개한다. 0%로 확정하지
         않는다(Codex P1-1: 실제 -10% 폭락이 0%로 삭제되던 경로).

    판정은 **키별로 독립**이다. A만 이상해도 계좌·B의 정상 수익을 지우지 않는다
    (Codex P1-2).
    """
    prev = day.setdefault("nav_prev", {})
    wealth = day.setdefault("wealth", {"account": 1.0, "A": 1.0, "B": 1.0})
    unknown = set(day.get("unresolved") or [])
    holds: dict = day.setdefault("hold_ticks", {})
    if not isinstance(holds, dict):                 # 옛 스칼라 상태 호환
        holds = {}
        day["hold_ticks"] = holds

    def shown() -> dict[str, float | None]:
        # hold(검증 중) 키도 숫자로 보여주지 않는다 — A를 의심하는 동안 A가
        #   포함된 account를 확정 숫자·색상으로 내보내면 사용자가 최대 15분간
        #   유령 변동을 본다(Codex V4 P2-1). 해소되면 숫자로 복구된다.
        veiled = unknown | set(day.get("pending_keys") or [])
        return {k: (None if k in veiled
                    else (float(wealth.get(k, 1.0)) - 1.0) * 100.0)
                for k in _KEYS}

    if not settled:
        day["accounting_pending"] = True
        for key in _KEYS:
            holds[key] = int(holds.get(key) or 0) + 1
        return shown()
    day["accounting_pending"] = False

    # 세션 첫 표본(아직 시리즈가 비어 있음)만 전일종가 갭 구간이다. carry 모드에선
    #   기준선이 이미 채워져 있으므로 기준선 유무로는 구분할 수 없다.
    first = not day.get("series")
    limit = _interval_limit(first and day.get("basis") == "previous_close")
    # 두 순회 사이 입력 변이 가능성을 코드로 제거한다(Codex 권고).
    snapshot = {
        key: (float((nav.get(key) or {}).get("value") or 0),
              float((nav.get(key) or {}).get("flow") or 0))
        for key in _KEYS
    }

    pending: list[dict] = []
    quarantined: list[dict] = []
    for key in _KEYS:
        cur_value, cur_flow = snapshot[key]
        old = prev.get(key)
        if not (old and float(old.get("value") or 0) > 0):
            prev[key] = {"value": cur_value, "flow": cur_flow}
            holds[key] = 0
            continue
        base = float(old["value"])
        external = cur_flow - float(old.get("flow") or 0)
        interval = (cur_value - base - external) / base
        row = {
            "key": key, "interval": round(interval, 6), "limit": limit,
            "prev_value": base, "prev_flow": float(old.get("flow") or 0),
            "cur_value": cur_value, "cur_flow": cur_flow,
            **(context or {}),
        }
        if math.isfinite(interval) and abs(interval) <= limit:
            wealth[key] = float(wealth.get(key, 1.0)) * (1.0 + interval)
            prev[key] = {"value": cur_value, "flow": cur_flow}
            holds[key] = 0
            continue
        held = int(holds.get(key) or 0)
        if held < _ANOMALY_HOLD_TICKS:
            # 기준선을 붙들면 시차인 경우 다음 틱에 원래 기준선에서 상쇄된다.
            holds[key] = held + 1
            pending.append(row)
            continue
        # 시차가 아니다. 실제인지도 증명 못 한다 → 이 키의 세션 값을 미확정 격리.
        unknown.add(key)
        holds[key] = 0
        prev[key] = {"value": cur_value, "flow": cur_flow}
        quarantined.append(row)

    # account는 A+B의 종속값이다 — 슬리브 하나가 미확정이면 그 의심 변화를
    #   가중 포함한 account도 증명이 없다. "한계 안"이라는 이유로 확정하면
    #   사용자가 가장 크게 보는 계좌-지수 비교에 유령 손익이 남는다(Codex V3
    #   P1-1). 독립 총 NAV 원천이 생기기 전까지 함께 미확정으로 전파한다.
    if unknown & {"A", "B"}:
        unknown.add("account")
    day["unresolved"] = sorted(unknown)
    # hold 중인 슬리브가 있으면 account도 '검증 중'으로 전파한다(P2-1). account
    #   자체 구간이 한계 안이어도, 의심 슬리브의 변화를 가중 포함한 값이다.
    pending_keys = {row["key"] for row in pending}
    if pending_keys & {"A", "B"}:
        pending_keys.add("account")
    day["pending_keys"] = sorted(pending_keys)
    if pending:
        day["anomaly_pending"] = pending
    else:
        day.pop("anomaly_pending", None)
    if pending:
        _alert_interval_anomaly(day, pending, stage="hold")
    if quarantined:
        _alert_interval_anomaly(day, quarantined, stage="quarantine")
    day["nav_last"] = {key: dict(value) for key, value in prev.items()}
    return shown()


def _alert_interval_anomaly(day: dict, rows: list[dict], *, stage: str) -> None:
    """단계별로 한 번씩 알린다. 원본 수치를 남겨 사후 진단이 가능하게.

    보류(hold)와 미확정 격리(quarantine)는 **서로 다른 사건**이므로 알림
    플래그를 분리한다. 종전에는 첫 보류 알림이 최종 격리 알림을 삼켰다
    (Codex P2-3 — 사람이 '잠시 보류' 통보만 받고 결과를 못 받음).
    """
    forensic = day.setdefault("anomaly_log", [])
    if len(forensic) < 40:
        forensic.append({"stage": stage, "items": rows})
    _quality_append({"ev": stage, "items": rows})       # 세션 교체에도 영속(P2-3)
    flags = day.setdefault("anomaly_notified", {})
    if not isinstance(flags, dict):
        flags = {}
        day["anomaly_notified"] = flags
    if flags.get(stage):
        return
    flags[stage] = True
    worst = max(rows, key=lambda row: abs(row["interval"]))
    if stage == "quarantine":
        tail = ("실제 손익인지 데이터 오류인지 증명하지 못해 <b>미확정</b>으로 "
                "격리했습니다. 이 세션·기간 통계에서 계좌 수익률은 숫자 대신 "
                "미확정으로 표시되며 장기 누적에도 넣지 않습니다. 수동 확인 필요.")
    else:
        tail = "누적을 보류하고 기준선을 유지합니다 — 시차면 자동 해소됩니다."
    try:
        notify.send(
            f"⚠️ <b>성과 계산 이상 구간</b> — {worst['key']} 한 틱 "
            f"{worst['interval'] * 100:+.2f}% (한계 {worst['limit'] * 100:.1f}%). "
            f"{tail}", critical=True, category="trade")
    except Exception:
        pass


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
        # 전날 미확정 **키**의 기준선만 버린다(P1-2 키별 독립). account가 미확정
        #   이었으면 계좌-지수 비교 기준이 어긋나므로 전체를 첫 표본 0%로 재시작
        #   (P1-3 — 계좌는 어제 기준·지수는 오늘 전일종가 기준이 되는 혼입 차단).
        raw_unres = (carry or {}).get("unresolved")
        carry_unres = (set(_KEYS) if raw_unres is True
                       else set(raw_unres or []))       # 구버전 bool 호환
        use_carry = bool(carry and nav_mode and "account" not in carry_unres)
        day = {"date": today, "pl0": tot_pl,
               "a_pl0": agg[mkt]["A"]["pl"], "b_pl0": agg[mkt]["B"]["pl"],
               # carry(전일종가 기준) 세션에서 전일종가가 없는 지수는 기준을
               #   **현재값으로 대체하지 않는다**(Codex V4 P1-2 — 그렇게 하면
               #   계좌는 전일 기준, 지수는 0%가 되어 모르는 값이 quality=ok로
               #   장기 복리된다). None 기준 = 그 지수는 미확정으로 표시한다.
               "idx0": {
                   k: ((idx_previous_close or {}).get(k) if use_carry else v)
                   for k, v in idx.items()},
               "series": [],
               "series_v2": [],
               "opened": True, "closed": False,
               "calc_version": 3 if nav_mode else 2,
               "basis": ("previous_close" if use_carry else "first_sample"),
               "holding_start_codes": sorted(holding_start_codes or set())}
        # 장중 재기준으로 새로 시작한 세션은 '하루'가 아니다 — 부분 세션을 한
        #   거래일처럼 장기 누적에 넣지 않는다(Codex P2-1).
        if ((st.get("reanchored") or {}).get(mkt) or {}).get("date") == today:
            day["partial_session"] = True
        if use_carry:
            # 미확정 키는 carry 기준선을 이어받지 않고 첫 표본에서 새로 시작한다.
            day["nav_prev"] = {
                key: dict(value)
                for key, value in (carry.get("nav_last") or {}).items()
                if key not in carry_unres}
            day["wealth"] = {"account": 1.0, "A": 1.0, "B": 1.0}
        st["day"][mkt] = day
    day.setdefault("a_pl0", agg[mkt]["A"]["pl"])
    day.setdefault("b_pl0", agg[mkt]["B"]["pl"])
    day.setdefault("idx0", {k: v for k, v in idx.items()})
    day.setdefault("series", [])
    day.setdefault("series_v2", [])
    day.setdefault("holding_start_codes", sorted(holding_start_codes or set()))
    if nav_mode:
        twr = _twr_step(day, nav, settled=accounting_settled,
                        context={"t": now_hhmm, "mkt": mkt, "date": today,
                                 "positions": len(holding_start_codes or ()),
                                 "settled": bool(accounting_settled),
                                 "unaccounted": ((st.get("diag") or {})
                                                 .get(mkt) or {}).get("unaccounted"),
                                 "fx": (1.0 if mkt == "KR"
                                        else float(settings.FX_USDKRW))})
        acct, a, b = twr["account"], twr["A"], twr["B"]
    else:
        acct = _pct(tot_pl - day["pl0"], tot_cost)
        a = _pct(agg[mkt]["A"]["pl"] - day["a_pl0"], agg[mkt]["A"]["cost"])
        b = _pct(agg[mkt]["B"]["pl"] - day["b_pl0"], agg[mkt]["B"]["cost"])
    ipct: dict[str, float | None] = {}
    carry_basis = day.get("basis") == "previous_close"
    for name, v in idx.items():
        v0 = day["idx0"].get(name)
        if not v0:
            if carry_basis:
                # 전일종가 기준 세션 — 기준 없는 지수를 현재값 0%로 확정하지
                #   않는다. 늦게라도 전일종가가 오면 그때 정당한 기준으로 고정.
                late = (idx_previous_close or {}).get(name)
                if late:
                    day["idx0"][name] = v0 = late
                else:
                    day["idx0"][name] = None
                    ipct[name] = None
                    continue
            else:                               # 첫표본 기준 — 그 틱이 0% 기준
                day["idx0"][name] = v0 = v
        ipct[name] = (v / v0 - 1) * 100.0
    # 미확정(None)은 0으로 낮춰 표시하지 않는다 — 숫자로 보이는 순간 확정된다.
    # 지수 자리는 **주 지수 전용**: 주 지수가 결측이면 None을 기록한다. 다른
    #   지수를 대신 넣으면 화면·마감·일별 행이 그 값을 주 지수 이름으로 부른다
    #   (Codex V3 P1-3 — 나스닥 결측일에 S&P500이 나스닥으로 둔갑).
    primary_name = IDX[mkt][0][1]
    primary_val = ipct.get(primary_name)
    day["series"].append([now_hhmm, (None if acct is None else round(acct, 3)),
                          (None if primary_val is None
                           else round(primary_val, 3))])
    if day.get("basis") == "first_sample":
        # 리베이스 첫날 계좌 TWR과 동일한 첫 관측값을 지수의 일간 기준으로 쓴다.
        # 전일종가 갭을 지수에만 넣으면 1m/3m/전체에 영구 복리되는 비교 오차가 난다.
        daily_indices = dict(ipct)
    else:
        # 전일종가가 없는 지수는 **명시적 None** — 키를 빼면 소비자가 세션
        #   기준값으로 폴백해 기준이 다른 값을 일간 수익률로 쓴다(Codex V4 P1-2).
        daily_indices = {
            name: ((value / (idx_previous_close or {}).get(name) - 1) * 100
                   if (idx_previous_close or {}).get(name) else None)
            for name, value in idx.items()
        }
    rnd = lambda v: None if v is None else round(v, 4)
    point = {
        "t": now_hhmm,
        "account": rnd(acct),
        "A": rnd(a),
        "B": rnd(b),
        "unresolved": list(day.get("unresolved") or []),
        "pending": list(day.get("pending_keys") or []),
        "indices": {name: rnd(value) for name, value in ipct.items()},
        "holdings": {
            key: (round(value, 4) if value is not None else None)
            for key, value in (holdings_daily or {}).items()
        },
        "daily_indices": {
            name: rnd(value) for name, value in daily_indices.items()
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
    """상승/하락 캡처 + 지수 대비 승률. 표본<5면 빈 문자열.

    품질 행(acct=None — 미확정·미정산·부분세션)은 표본에서 제외한다. 제외
    자체가 편향일 수 있으므로 제외 일수를 함께 표기한다(사용자가 볼 수 있게).
    """
    all_rows = [d for d in days if d.get("mkt") == mkt]
    rows = [d for d in all_rows if d.get("acct") is not None
            and d.get("idx") is not None]
    dropped = len(all_rows) - len(rows)
    suffix = f" · 미확정 제외 {dropped}일" if dropped else ""
    if len(rows) < 5:
        # 표본 초기에도 제외 일수는 숨기지 않는다(Codex V3 P2-1 — 편향 가시화).
        return (f"누적 {len(rows)}일 — 5일부터 캡처 통계 표시{suffix}"
                if all_rows else "")
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
    return " · ".join(parts) + suffix


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
            # 구형 series 폴백 — None(미확정·주지수 결측)을 0으로 강등하거나
            #   float(None)으로 죽지 않는다(Codex V3 P2-2). 손상 행만 제외.
            primary = IDX[market][0][1]
            series = []
            for row in (day.get("series") or []):
                if not (isinstance(row, list) and len(row) >= 3):
                    continue
                try:
                    account = None if row[1] is None else float(row[1])
                    idx_val = None if row[2] is None else float(row[2])
                except (TypeError, ValueError):
                    continue                     # 손상 행 — fail-closed 제외
                series.append({"t": row[0], "account": account,
                               "A": None, "B": None,
                               "indices": {primary: idx_val}})
        markets[market] = {
            "label": labels[market],
            "date": day.get("date"),
            "basis": day.get("basis") or "first_sample",
            "indices": [name for _symbol, name in IDX[market]],
            "series": series[-SERIES_MAX * 4:],
        }
    days = []
    for row in (st.get("days") or [])[-800:]:   # 시장별 400행 보관과 정합(P2-5)
        market = row.get("mkt")
        if market not in ("US", "KR"):
            continue
        indices = dict(row.get("indices") or {})
        if not indices and row.get("idx") is not None:
            indices[IDX[market][0][1]] = float(row.get("idx") or 0)
        out_row = {
            "date": row.get("d"),
            "market": market,
            "basis": row.get("basis") or "previous_close",
            # 미확정(None)은 0으로 낮추지 않는다 — 소비 측이 누적을 끊는 신호다.
            "account": (float(row["acct"]) if row.get("acct") is not None
                        else None),
            "quality": row.get("quality") or "ok",
            "unresolved_keys": list(row.get("unresolved_keys") or []),
            "A": (float(row["a"]) if row.get("a") is not None else None),
            "B": (float(row["b"]) if row.get("b") is not None else None),
            # 지수별 None(전일종가 결측 등 미확정)을 보존 — float(None) 금지.
            "indices": {str(k): (float(v) if v is not None else None)
                        for k, v in indices.items()},
            "holdings": dict(row.get("holdings") or {}),
        }
        # daily_indices 키는 **원본 행에 있을 때만** 내보낸다. 항상 {}를 실으면
        #   소비자가 구버전 행과 '명시적 결측' 행을 구분하지 못해 세션 기준값
        #   폴백이 되살아난다(Codex V4 P1-2 — `?? indices` 세탁 경로).
        row_daily = row.get("daily_indices")
        if isinstance(row_daily, dict):
            out_row["daily_indices"] = {
                str(k): (float(v) if v is not None else None)
                for k, v in row_daily.items()}
        days.append(out_row)
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
KEY_LABEL = {"account": "계좌", "A": "전략A", "B": "전략B"}


def _fmt(v: float | None) -> str:
    """미확정(None)은 숫자로 낮추지 않는다 — 숫자로 보이는 순간 확정이 된다."""
    return "미확정" if v is None else f"{v:+.2f}%"


def tick(now: datetime.datetime | None = None) -> None:
    """매수루프가 5분마다 호출. 세션 중 스냅샷·알림, 세션 종료 시 마감 요약.

    상태 파일 읽기-수정-쓰기 전체를 잠금 안에서 수행한다 — 유지보수 스크립트와
    겹쳐 서로의 갱신을 덮어쓰지 않게(Codex P2-2). 잠금을 timeout 안에 못 잡으면
    이번 성과 틱만 건너뛴다(매수 사이클을 막지 않기 위해 — 알림은 1회).
    """
    global _lock_skip_alerted
    try:
        with state_lock():
            _tick_locked(now)
    except TimeoutError as exc:
        if not _lock_skip_alerted:
            _lock_skip_alerted = True
            try:
                notify.send(f"⚠️ 성과 추적 잠금 대기 초과 — {exc}. 매수는 정상,"
                            " 성과 표본만 이번 주기 누락.", category="trade")
            except Exception:
                pass
        return
    _lock_skip_alerted = False


_lock_skip_alerted = False


def _tick_locked(now: datetime.datetime | None = None) -> None:
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
            elif day and day.get("close_alert_pending"):
                _deliver_close_alert(st, mkt, day)   # 실패 알림 재시도(P2-4)
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
            unaccounted = ledger.unaccounted_fills()
            settled = unaccounted == 0
        except Exception:
            unaccounted = None
            settled = False                  # 원장 판정 불가 = 누적 보류(fail-closed)
        st.setdefault("diag", {})[mkt] = {"unaccounted": unaccounted}
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
        want = ("first" if first
                else "mid" if time.time() - last_alert >= ALERT_MIN * 60 - 90
                else None)
        # 네트워크 알림보다 **먼저** 상태를 원자 저장한다. 알림 예외가 격리·표본·
        #   sampled_at 저장을 날리면 다음 사이클마다 같은 격리를 반복한다.
        _save(st)
        if want:
            ok = False
            try:
                if want == "first":
                    ok = bool(notify.send(
                        f"📊 <b>성과 추적 시작</b> "
                        f"({'미장' if mkt == 'US' else '국장'})"
                        f" — 세션 기준점 설정. 1시간마다 지수 대비 비교 알림."))
                else:
                    ok = _mid_alert(st, mkt, r)
            except Exception:
                ok = False
            if ok:                             # 전달 성공 후에만 시각 갱신 —
                st["alert"][mkt] = time.time() # 실패면 다음 주기 실제 재시도(P2-4)
                _save(st)
        try:
            publish_dash(st)                   # 웹 대시보드(perf.html)용 발행
        except Exception:
            pass


def _vs_line(acct: float | None, ipct: dict) -> str:
    idx_txt = " · ".join(f"{k} {_fmt(v)}" for k, v in ipct.items())
    if acct is None:
        # 미확정이면 초과수익·색상 판정을 하지 않는다(P1-1).
        return f"내 계좌 미확정 vs {idx_txt}\n→ 지수 대비 판정 보류(수동 확인 필요)"
    main = next(iter(ipct.values()), None)
    if main is None:
        # 주 지수 미확정(전일종가 결측 등) — null을 0처럼 빼지 않는다(V4 P1).
        return f"내 계좌 {_fmt(acct)} vs {idx_txt}\n→ 지수 대비 판정 보류(지수 미확정)"
    d = acct - main
    mark = "🟢" if d >= 0 else "🔴"
    return f"내 계좌 {_fmt(acct)} vs {idx_txt}\n→ 지수 대비 {mark} {d:+.2f}%p"


def _mid_alert(st: dict, mkt: str, r: dict) -> bool:
    name = "미장·나스닥" if mkt == "US" else "국장·코스피/코스닥"
    body = (f"📊 <b>성과 vs 지수</b> ({name}, 장중)\n"
            + _vs_line(r["acct"], r["idx"])
            + f"\n전략별: A(전환) {_fmt(r['a'])} · B(매물대) {_fmt(r['b'])}")
    idx_name = next(iter(r["idx"].keys()), "지수")
    url = chart_url(r["series"], idx_name, f"오늘 장중 추이 vs {idx_name}")
    return bool(notify.send_photo(url, body) or notify.send(body))


def _close_alert(st: dict, mkt: str, day: dict) -> None:
    if not day["series"]:
        return
    last = day["series"][-1]
    acct, ipct = last[1], last[2]
    rich = (day.get("series_v2") or [{}])[-1]
    days = st.setdefault("days", [])
    unresolved = sorted(set(day.get("unresolved") or []))
    acc_pending = bool(day.get("accounting_pending") or day.get("anomaly_pending"))
    partial = bool(day.get("partial_session"))
    # 품질 등급 — 미정산·부분세션은 전 키가 의심, unresolved는 해당 키만.
    quality = ("partial" if partial else "pending" if acc_pending
               else "unresolved" if unresolved else "ok")

    def _val(key: str, value):
        """키별 값 — 미확정 키·전체 의심 등급은 None(숫자로 확정 금지)."""
        if quality in ("partial", "pending") or key in unresolved:
            return None
        return value

    # **모든 거래일에 품질 행을 남긴다**(Codex P1-3). 미확정 하루가 역사에서
    #   사라지면 그 전후 정상일이 하나의 연속 TWR처럼 복리돼 장기 비교가
    #   거짓이 된다. 소비 측은 quality != ok 행에서 누적을 끊어야 한다.
    days.append({
        "d": day["date"], "mkt": mkt,
        "acct": _val("account", acct), "idx": ipct,
        "basis": day.get("basis") or "first_sample",
        "a": _val("A", rich.get("A")), "b": _val("B", rich.get("B")),
        "quality": quality,
        "unresolved_keys": unresolved,
        "indices": rich.get("indices") or {IDX[mkt][0][1]: ipct},
        "holdings": rich.get("holdings") or {},
        "daily_indices": rich.get("daily_indices") or {},
    })
    _days_ledger_append(days[-1])           # 400일 창 밖으로 밀려도 원본 보존(P2-2)
    if day.get("nav_last"):
        # 미확정 **키**의 기준선만 다음 세션이 버리게 키 집합으로 싣는다(P1-2 —
        #   A만 이상인데 계좌·B의 carry까지 포기하면 정상 키 성과가 또 끊긴다).
        carry_unres = (sorted(_KEYS) if quality in ("partial", "pending")
                       else unresolved)
        st.setdefault("carry", {})[mkt] = {
            "date": day.get("date"), "nav_last": day["nav_last"],
            "unresolved": carry_unres,
        }
    # 시장별 장기 보관 — 두 시장 합산 120행 컷은 '전체' 비교 창을 시장당 약
    #   60거래일(≈3개월)로 줄였다(Codex V3 P2-5). 시장별 400거래일(≈1.6년)씩
    #   보관하고 공개 payload는 발행부에서 따로 자른다.
    keep: set[int] = set()
    for market in ("US", "KR"):
        market_rows = [i for i, r in enumerate(days) if r.get("mkt") == market]
        keep.update(market_rows[-DAYS_RETENTION:])
    keep.update(i for i, r in enumerate(days) if r.get("mkt") not in ("US", "KR"))
    st["days"] = days = [r for i, r in enumerate(days) if i in keep]
    if quality != "ok":
        _quality_append({
            "ev": "close", "mkt": mkt, "date": day.get("date"),
            "quality": quality, "unresolved_keys": unresolved,
            # 마감 원인 분석용 진단 스냅샷 — anomaly 없이 미정산만으로 끝난
            #   날에도 미회계 수·환율이 남게(Codex V3 P2-3).
            "unaccounted": ((st.get("diag") or {}).get(mkt) or {})
            .get("unaccounted"),
            "positions": len(day.get("holding_start_codes") or []),
            "fx": (1.0 if mkt == "KR" else float(settings.FX_USDKRW)),
            "accounting_pending": bool(day.get("accounting_pending")),
            "anomaly_log": (day.get("anomaly_log") or [])[-10:],
        })
    cap = capture_stats(days, mkt)
    name = "미장·나스닥" if mkt == "US" else "국장·코스피"
    if unresolved:
        labels = ", ".join(KEY_LABEL.get(k, k) for k in unresolved)
        ok_keys = [k for k in ("account", "A", "B") if k not in unresolved]
        ok_txt = " · ".join(
            f"{KEY_LABEL[k]} {_fmt(acct if k == 'account' else rich.get(k))}"
            for k in ok_keys)
        body = (f"🏁 <b>장 마감 성과</b> ({name})\n"
                f"❓ <b>{labels} 미확정</b> — 실제 손익인지 데이터 오류인지 "
                f"증명하지 못했습니다(지수 {_fmt(ipct)})\n"
                + (f"정상 확정: {ok_txt}\n" if ok_txt else "")
                + "미확정 키는 0%로 낮추지 않았고 장기 누적에서도 숫자로 "
                  "확정하지 않습니다. 수동 확인 필요.")
    elif quality == "pending":
        body = (f"🏁 <b>장 마감 성과</b> ({name})\n"
                f"⏳ 매도 회계 정산 대기 — 계좌 수익률 산정 보류"
                f"(지수 {_fmt(ipct)})\n"
                "정산되면 다음 세션부터 이어집니다. 누적에는 품질 행으로만 남깁니다.")
    elif quality == "partial":
        body = (f"🏁 <b>장 마감 성과</b> ({name})\n"
                f"↺ 재기준 부분 세션 — 하루 성과로 집계하지 않음"
                f"(지수 {_fmt(ipct)})")
    elif ipct is None:
        # 주 지수 결측 마감 — 계좌는 확정하되 지수 비교는 판정하지 않는다.
        body = (f"🏁 <b>장 마감 성과</b> ({name})\n"
                f"오늘: 내 계좌 {_fmt(acct)} · 주 지수 결측 — 지수 대비 판정 보류")
    else:
        d = acct - ipct
        mark = "🟢" if d >= 0 else "🔴"
        body = (f"🏁 <b>장 마감 성과</b> ({name})\n"
                f"오늘: 내 계좌 {_fmt(acct)} vs 지수 {_fmt(ipct)} "
                f"→ {mark} {d:+.2f}%p\n"
                + (f"누적 통계: {cap}" if cap
                   else "누적 통계: 5일 이상 쌓이면 상승/하락 캡처 표시"))
    # 네트워크 알림 전에 상태를 원자 저장 — 알림 예외가 마감 기록을 날려 같은
    #   마감을 반복하지 않게 한다. 알림 전달 자체는 outbox로 재시도한다(V3 P2-4).
    day["close_alert_body"] = body
    day["close_alert_pending"] = True
    _save(st)
    _deliver_close_alert(st, mkt, day)


def _deliver_close_alert(st: dict, mkt: str, day: dict) -> None:
    """마감 알림 전달 — 성공 시에만 pending 해제(다음 틱이 재시도).

    재시도·포기는 **횟수가 아니라 실제 경과 시간** 기준이다(Codex V4 P3-1 —
    장외 분기는 5분 sample gate 앞이라 buyloop 1분 주기면 12회가 12분이었다).
    5분 간격으로 재시도하고, 최초 실패 후 1시간이 지나면 포기하되 운영 경보와
    품질 원장에 남긴다. `close_alert_body`는 forensic 목적으로 보존한다.
    """
    body = day.get("close_alert_body")
    if not body or not day.get("close_alert_pending"):
        return
    now_ts = time.time()
    next_at = float(day.get("close_alert_next_at") or 0)
    if now_ts < next_at:
        return                              # 재시도 간격(5분) 미도달 — 대기
    first_fail = float(day.get("close_alert_first_fail_at") or 0)
    if first_fail and now_ts - first_fail >= 3600:
        day["close_alert_pending"] = False  # body는 사후 진단용으로 남긴다
        _quality_append({"ev": "close_alert_giveup", "mkt": mkt,
                         "date": day.get("date"),
                         "first_fail_at": first_fail,
                         "tries": int(day.get("close_alert_tries") or 0)})
        try:
            notify.send(f"⚠️ 마감 성과 알림 1시간 재시도 실패 — 포기"
                        f"({mkt} {day.get('date')}). 상태 파일에 본문 보존됨.",
                        category="trade")
        except Exception:
            pass
        _save(st)
        return
    day["close_alert_tries"] = int(day.get("close_alert_tries") or 0) + 1
    ok = False
    try:
        idx_name = "나스닥" if mkt == "US" else "코스피"
        url = chart_url(day["series"], idx_name, f"{day['date']} 세션 추이")
        ok = bool(notify.send_photo(url, body) or notify.send(body))
    except Exception:
        ok = False
    if ok:
        day["close_alert_pending"] = False
        day.pop("close_alert_body", None)
        day.pop("close_alert_first_fail_at", None)
        day.pop("close_alert_next_at", None)
    else:
        day.setdefault("close_alert_first_fail_at", now_ts)
        day["close_alert_next_at"] = now_ts + 300
    _save(st)
