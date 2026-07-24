"""주문 원장(append-only) + 상태기계 + 대사(reconcile) — 실매매 초과매도 방지.

실거래에서 가장 위험한 경로: 손절 주문을 보냈는데 응답이 timeout이면, 체결됐는지
안 됐는지 모른 채(UNKNOWN) 재주문하면 **초과 매도**가 난다. 이 원장은:

  submitted → filled                     (정상 체결)
            → partial(부분체결) → 잔여만 재주문
            → rejected                   (거부 — 종료)
            → unknown(타임아웃) → 해당 종목 잠금 → 대사 후에만 잔여 재주문

핵심 규칙:
  · UNKNOWN이면 그 종목을 **잠근다** — 대사(브로커 실보유·미체결 대사)로 실제
    체결량을 확인하기 전엔 어떤 새 주문도 금지(초과매도 원천 차단).
  · 재주문은 **잔여 수량만**(intended − 이미 체결). 절대 원수량 재발주 금지.
  · append-only JSONL — 크래시·재시작에도 주문 이력이 남아 대사 가능.
  · 원장 flock은 재귀 잠금이 아니다. 이미 원장 락을 쥔 호출 경로는 반드시
    ``_fold_unlocked``/``_append_unlocked``만 사용한다. 잠금 계층은
    ``ledger > {costbook, kis_positions}`` 단방향이며 네트워크 호출 중에는
    어떤 파일 락도 보유하지 않는다.

Stage 0/모의에선 브로커가 '항상 체결(filled)'을 돌려주므로 이 기계는 지나가기만
하지만, 토스 실주문 붙일 때 UNKNOWN/부분체결/대사 경로가 이미 검증돼 있게 만든다.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import threading
import time

LEDGER_PATH = "bot/order_ledger.jsonl"

# 종료 상태(더 이상 잔여 없음) vs 진행 상태
_TERMINAL = {"filled", "rejected", "cancelled", "expired", "dry_run"}
# in-flight(결과 미확정) — 부분체결도 원주문 잔량이 살아 있으면 새 주문을 막아야 한다.
_INFLIGHT = {"submitted", "ack", "partial", "cancel_pending", "unknown"}

# 대사 신뢰도(리뷰 R3) — 후보가 유일하면 HIGH(자동확정), 모호하면 LOW(수동 잠금 유지)
CONF_HIGH = "high"
CONF_LOW = "low"
_THREAD_LOCK = threading.RLock()


@contextmanager
def _file_lock(exclusive: bool):
    """원장 읽기/쓰기의 호스트 프로세스 공용 잠금."""
    lock_path = LEDGER_PATH + ".lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with _THREAD_LOCK:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _append_unlocked(ev: dict) -> None:
    """잠금 보유 상태에서 한 이벤트를 append하고 디스크까지 확정한다."""
    ev.setdefault("ts", time.time())
    parent = os.path.dirname(LEDGER_PATH) or "."
    os.makedirs(parent, exist_ok=True)
    existed = os.path.exists(LEDGER_PATH)
    payload = (json.dumps(ev, ensure_ascii=False, separators=(",", ":"))
               + "\n").encode("utf-8")
    fd = os.open(LEDGER_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.chmod(LEDGER_PATH, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("ledger append made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if not existed:
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)                         # 새 파일 이름도 crash-safe
        finally:
            os.close(dfd)


def _append(ev: dict) -> None:
    with _file_lock(True):
        _append_unlocked(ev)


def _fold_unlocked() -> tuple[dict, list[int]]:
    """원장을 재생성 — key별 현재 상태로 접는다. {key: {...}}.
    filled는 '지금까지 확인된 총 체결량'(절대값).

    JSON 손상 줄은 무시해 거래를 계속하지 않고 줄 번호를 함께 반환한다. 호출부의
    주문 게이트는 손상이 하나라도 있으면 전면 fail-closed한다.
    """
    st: dict = {}
    corrupt: list[int] = []
    try:
        with open(LEDGER_PATH, encoding="utf-8") as fp:
            for lineno, line in enumerate(fp, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    corrupt.append(lineno)
                    continue
                if not isinstance(ev, dict):
                    corrupt.append(lineno)
                    continue
                key = ev.get("key")
                if not key:
                    corrupt.append(lineno)
                    continue
                cur = st.setdefault(key, {"symbol": ev.get("symbol"),
                                          "intended": 0, "filled": 0,
                                          "state": "submitted",
                                          "reconciled": False})
                if ev.get("symbol"):
                    cur["symbol"] = ev["symbol"]
                if "intended" in ev:
                    cur["intended"] = ev["intended"]
                if "filled" in ev and ev["filled"] is not None:
                    cur["filled"] = ev["filled"]     # 절대값(누적 아님)
                if ev.get("state"):
                    cur["state"] = ev["state"]
                if ev.get("ev") == "reconcile":
                    cur["reconciled"] = True
                # KIS 확장 필드 — 체결·회계·취소 상태까지 append-only로 보존한다.
                for f in ("odno", "orgno", "ord_tmd", "synthetic", "confidence",
                          "fill_price", "fill_price_source", "open", "accounted"):
                    if f in ev and ev.get(f) is not None:
                        cur[f] = ev[f]
                if ev.get("ev") == "submit":
                    cur["submitted_at"] = ev.get("ts", 0.0)
                    meta = ev.get("meta") or {}
                    for f in ("side", "excg", "market", "price", "hldg_before",
                              "pos_key", "sleeve", "fx", "ccy", "stop", "target",
                              "name", "opened", "tactic", "pending", "parent_key",
                              "chase", "ref_price", "reason",
                              "reservation_cost_krw",
                              "budget_total_held_krw", "budget_total_limit_krw",
                              "budget_sleeve_held_krw", "budget_sleeve_limit_krw"):
                        if f in meta and meta.get(f) is not None:
                            cur[f] = (str(meta[f]).upper() if f == "side" else meta[f])
                elif ev.get("ev") == "plan":
                    cur["created_at"] = ev.get("ts", 0.0)
                    meta = ev.get("meta") or {}
                    for f, value in meta.items():
                        if value is not None:
                            cur[f] = value
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        corrupt.append(-1)
    return st, corrupt


def _fold() -> dict:
    """진단/조회용 상태 재생성. 주문 허용 여부는 반드시 ledger_healthy도 본다."""
    with _file_lock(False):
        return _fold_unlocked()[0]


def corruption_status() -> dict:
    with _file_lock(False):
        _, lines = _fold_unlocked()
    return {"healthy": not lines, "lines": lines, "path": LEDGER_PATH}


def ledger_healthy() -> bool:
    return bool(corruption_status()["healthy"])


def record_submit(key: str, symbol: str, qty: int, reason: str = "",
                  meta: dict | None = None) -> None:
    """주문 전송 직전 기록. 반드시 send() 이전에 호출(크래시 대비)."""
    _append({"ev": "submit", "key": key, "symbol": symbol,
             "intended": int(qty), "filled": 0, "state": "submitted",
             "reason": reason, "meta": meta or {}})


def _can_submit_fold(fold: dict, symbol: str, min_interval_s: float,
                     now: float, exclude_key: str | None = None) -> bool:
    for key, cur in fold.items():
        if key == exclude_key or cur.get("symbol") != symbol:
            continue
        if (cur.get("state") == "unknown" and not cur.get("reconciled")):
            return False
        if (cur.get("state") in _INFLIGHT
                and not (cur.get("state") == "partial"
                         and cur.get("open") is False)):
            return False
    latest = max(
        [float(v.get("submitted_at", 0.0)) for key, v in fold.items()
         if key != exclude_key and v.get("symbol") == symbol] or [0.0])
    return now - latest >= min_interval_s


def _buy_reservation_costs(fold: dict, exclude_key: str | None = None
                           ) -> tuple[float, dict[str, float]] | None:
    """원장의 열린 BUY/계획과 미회계 체결을 KRW 예약액으로 합산.

    BUY가 ``filled``로 끝나도 costbook 반영(``accounted``) 전에는 예약을
    해제하지 않는다. 그렇지 않으면 원장에서는 종료됐지만 브로커 잔고/원가장부에는
    아직 안 보이는 전환창에서 다른 프로세스가 총시드를 한 주문만큼 더 쓸 수 있다.
    """
    total = 0.0
    by_sleeve: dict[str, float] = {}
    for key, cur in fold.items():
        if key == exclude_key or str(cur.get("side") or "").upper() != "BUY":
            continue
        state = str(cur.get("state") or "")
        filled = max(0, int(cur.get("filled") or 0))
        accounted = max(0, int(cur.get("accounted") or 0))
        unaccounted = max(0, filled - accounted)
        terminal_handoff = state in _TERMINAL
        if terminal_handoff and unaccounted <= 0:
            continue
        if state == "partial" and cur.get("open") is False and unaccounted <= 0:
            continue
        parent_key = str(cur.get("parent_key") or "")
        parent = fold.get(parent_key) if parent_key else None
        # half 1차가 planned 전체수량을 reservation_cost_krw로 잡는 동안 2차
        # 계획을 다시 더하지 않는다. 1차가 종료되면 계획이 예약을 이어받는다.
        if (state == "planned" and parent
                and str(parent.get("state") or "") not in _TERMINAL
                and not (parent.get("state") == "partial"
                         and parent.get("open") is False)
                and float(parent.get("reservation_cost_krw") or 0) > 0):
            continue
        try:
            explicit = float(cur.get("reservation_cost_krw") or 0)
            if explicit > 0:
                if terminal_handoff or (
                        state == "partial" and cur.get("open") is False):
                    intended = max(1, int(cur.get("intended") or 0))
                    # 취소된 부분체결은 미회계 체결분만 유지한다. 완전체결은 기존
                    # worst-case 예약 전액을 costbook durable 기록까지 이어받는다.
                    cost = explicit * min(unaccounted, intended) / intended
                else:
                    cost = explicit
            else:
                qty = (unaccounted if terminal_handoff or (
                    state == "partial" and cur.get("open") is False)
                    else max(0, int(cur.get("intended") or 0) - filled))
                price = float(cur.get("price") or cur.get("limit") or 0)
                fx = float(cur.get("fx") or (1.0 if cur.get("market") == "KR" else 0))
                if qty > 0 and (price <= 0 or fx <= 0):
                    return None
                cost = qty * price * fx
        except (TypeError, ValueError):
            return None
        sleeve = str(cur.get("sleeve") or "A").upper()
        total += cost
        by_sleeve[sleeve] = by_sleeve.get(sleeve, 0.0) + cost
    return total, by_sleeve


def try_record_submit(key: str, symbol: str, qty: int, reason: str = "",
                      meta: dict | None = None, *,
                      min_interval_s: float = 60.0,
                      now: float | None = None) -> bool:
    """게이트 검사와 전송 전 기록을 한 flock 임계구역에서 처리한다.

    같은 순간 두 프로세스가 같은 종목을 검사해도 하나만 submit을 확보한다.
    원장 손상 또는 기존 key 재사용은 모두 False다.
    """
    stamp = time.time() if now is None else float(now)
    with _file_lock(True):
        fold, corrupt = _fold_unlocked()
        existing = fold.get(key)
        if corrupt or (existing is not None and existing.get("state") != "planned"):
            return False
        if not _can_submit_fold(
                fold, symbol, min_interval_s, stamp,
                exclude_key=key if existing is not None else None):
            return False
        m = meta or {}
        try:
            total_held = m.get("budget_total_held_krw")
            total_limit = m.get("budget_total_limit_krw")
            sleeve_held = m.get("budget_sleeve_held_krw")
            sleeve_limit = m.get("budget_sleeve_limit_krw")
            order_cost = float(m.get("reservation_cost_krw") or 0)
            if total_limit is not None or sleeve_limit is not None:
                # 호출부 스냅샷은 flock 밖에서 만들어졌을 수 있다. 같은 임계구역에서
                # durable costbook을 다시 읽어 max로 합쳐, 체결→회계 전환 직후에도
                # 오래된 잔고 스냅샷이 총시드 게이트를 우회하지 못하게 한다.
                from bot import costbook
                durable = costbook.budget_snapshot()
                if durable is None:
                    return False
                total_held = max(
                    float(total_held or 0), float(durable["total"]))
                sleeve = str(m.get("sleeve") or "A").upper()
                sleeve_held = max(
                    float(sleeve_held or 0),
                    float(durable["by_sleeve"].get(sleeve, 0.0)))
                reservations = _buy_reservation_costs(fold, exclude_key=key)
                if reservations is None or order_cost <= 0:
                    return False
                reserved_total, reserved_by_sleeve = reservations
                if (total_limit is not None
                        and float(total_held or 0) + reserved_total + order_cost
                        > float(total_limit) + 1e-6):
                    return False
                if (sleeve_limit is not None
                        and float(sleeve_held or 0)
                        + reserved_by_sleeve.get(sleeve, 0.0) + order_cost
                        > float(sleeve_limit) + 1e-6):
                    return False
        except (TypeError, ValueError):
            return False
        _append_unlocked({
            "ev": "submit", "key": key, "symbol": symbol,
            "intended": int(qty), "filled": 0, "state": "submitted",
            "reason": reason, "meta": m, "ts": stamp,
        })
        return True


def try_record_cancel(key: str, symbol: str, reason: str = "",
                      meta: dict | None = None, *,
                      attempt_group: str | None = None,
                      now: float | None = None) -> bool:
    """취소 요청 키를 프로세스 간 원자적으로 한 번만 선기록한다.

    취소는 같은 종목의 원주문이 열려 있어야 하므로 ``try_record_submit``의
    종목 in-flight 게이트를 재사용할 수 없다. 대신 원장 손상 시 전면 차단하고,
    동일 취소 키가 이미 한 번이라도 기록됐다면 재전송하지 않는다. ``attempt_group``
    안에서는 확정 거부(rejected) 뒤의 새 시도만 허용한다. submitted/unknown 또는
    이미 접수된(filled) 취소가 하나라도 있으면 새 HTTP를 막아 응답유실 시 중복
    취소를 내지 않는다.
    """
    stamp = time.time() if now is None else float(now)
    with _file_lock(True):
        fold, corrupt = _fold_unlocked()
        if corrupt or key in fold:
            return False
        group = str(attempt_group or "")
        if group:
            family = [
                cur for prior_key, cur in fold.items()
                if prior_key == group or prior_key.startswith(group + "#")
            ]
            if any(str(cur.get("state") or "") != "rejected" for cur in family):
                return False
        _append_unlocked({
            "ev": "submit", "key": key, "symbol": symbol,
            "intended": 0, "filled": 0, "state": "submitted",
            "reason": reason, "meta": meta or {}, "ts": stamp,
        })
        return True


def promote_stale_submitted(age_s: float, now: float | None = None) -> list[dict]:
    """응답 상태를 못 남긴 오래된 submitted를 UNKNOWN으로 승격한다.

    전송 직후 프로세스가 죽으면 submitted에서 멈출 수 있다. 이를 평범한 미체결로
    간주하면 같은 주문을 다시 보낼 위험이 있으므로, 부팅 시 UNKNOWN 잠금으로
    바꾼 뒤 nccs/ccnl/잔고 대사만이 해제하게 한다.
    """
    stamp = time.time() if now is None else float(now)
    promoted: list[dict] = []
    with _file_lock(True):
        fold, corrupt = _fold_unlocked()
        if corrupt:
            return promoted
        for key, cur in fold.items():
            if (cur.get("state") != "submitted"
                    or stamp - float(cur.get("submitted_at") or 0) < age_s):
                continue
            _append_unlocked({
                "ev": "stale_submitted", "key": key, "state": "unknown",
                "filled": int(cur.get("filled") or 0), "open": True,
                "reason": f"submitted 응답 미기록 {int(age_s)}초 초과",
                "ts": stamp,
            })
            promoted.append({"key": key, **cur, "state": "unknown"})
    return promoted


def on_result(key: str, state: str, filled_qty: int = 0, *,
              fill_price: float | None = None,
              fill_price_source: str = "", open_order: bool | None = None) -> None:
    """전송 결과 반영. state ∈ submitted/ack/partial/filled/rejected/unknown.
    filled_qty는 '지금까지 확인된 총 체결량'(절대값)."""
    ev = {"ev": "result", "key": key, "state": state,
          "filled": int(filled_qty)}
    if fill_price is not None:
        ev["fill_price"] = float(fill_price)
        ev["fill_price_source"] = fill_price_source or "broker"
    if open_order is not None:
        ev["open"] = bool(open_order)
    _append(ev)


def reconcile(key: str, actual_filled: int, *, fill_price: float | None = None,
              fill_price_source: str = "", open_order: bool = False) -> dict:
    """UNKNOWN/부분 주문을 브로커 실측 체결량으로 확정하고 잠금 해제.
    반환: {state, filled, residual}. residual>0이면 잔여만 재주문 대상."""
    cur = state_of(key) or {"intended": 0}
    intended = cur.get("intended", 0)
    actual = max(0, int(actual_filled))
    if actual >= intended and intended > 0:
        state = "filled"
    elif actual > 0:
        state = "partial"
    else:
        state = "rejected"
    ev = {"ev": "reconcile", "key": key, "state": state, "filled": actual,
          "open": bool(open_order)}
    if fill_price is not None:
        ev["fill_price"] = float(fill_price)
        ev["fill_price_source"] = fill_price_source or "broker"
    _append(ev)
    return {"state": state, "filled": actual,
            "residual": max(0, intended - actual),
            "fill_price": fill_price, "open": bool(open_order)}


def record_plan(key: str, symbol: str, qty: int, *, meta: dict) -> None:
    """아직 브로커에 보내지 않은 눌림 주문 의도. 주문 전 모든 게이트를 다시 거친다."""
    _append({"ev": "plan", "key": key, "symbol": symbol, "intended": int(qty),
             "filled": 0, "state": "planned", "meta": meta})


def finish_plan(key: str, state: str, reason: str = "") -> None:
    """대기 주문의 만료·전략 훼손·취소 확정을 기록한다."""
    if state not in ("cancelled", "expired", "rejected"):
        raise ValueError(f"invalid plan terminal state: {state}")
    cur = state_of(key) or {}
    _append({"ev": "plan_finish", "key": key, "state": state,
             "filled": int(cur.get("filled", 0)), "open": False,
             "reason": reason})


def mark_cancelled(key: str, reason: str = "") -> None:
    """브로커에서 원주문 취소가 확정된 뒤 잔량이 더 체결될 수 없음을 기록."""
    cur = state_of(key) or {}
    _append({"ev": "cancel_confirmed", "key": key, "state": "cancelled",
             "filled": int(cur.get("filled", 0)), "open": False,
             "reason": reason})


def mark_accounted(key: str, qty: int) -> None:
    """원가장부에 반영한 누적 체결수량. 재대사 때 같은 체결을 중복 회계하지 않는다."""
    _append({"ev": "accounted", "key": key, "accounted": max(0, int(qty))})


def pending_orders() -> list[dict]:
    """눌림 주문 계획과 브로커에 제출된 대기 주문. 종료된 계획은 제외."""
    return [{"key": k, **v} for k, v in _fold().items()
            if v.get("pending") and v.get("state") not in _TERMINAL]


def state_of(key: str) -> dict | None:
    return _fold().get(key)


def residual_qty(key: str, intended: int) -> int:
    """이 주문키로 아직 안 나간 수량 = intended − 확인된 체결. 없으면 intended."""
    cur = _fold().get(key)
    if not cur:
        return int(intended)
    return max(0, int(intended) - int(cur.get("filled", 0)))


# ── 포지션 단위 집계 ──────────────────────────────────────────────
#   한 포지션의 손절을 여러 번 시도할 수 있다(부분체결 후 잔여 재주문). 각 시도는
#   별도 주문키 `{pos_key}#{n}`로 기록하고, 포지션의 총 체결은 그 키들을 합산한다.
#   (같은 키로 재submit하면 체결 누적이 어긋나므로 시도마다 키를 분리한다.)

def _pos_keys(pos_key: str, fold: dict) -> list:
    return [k for k in fold if k == pos_key or k.startswith(pos_key + "#")]


def filled_for(pos_key: str) -> int:
    """포지션 전체에서 확인된 총 체결량(여러 주문 시도 합산)."""
    fold = _fold()
    return sum(int(fold[k].get("filled", 0)) for k in _pos_keys(pos_key, fold))


def attempts(pos_key: str) -> int:
    """이 포지션에 대해 지금까지 낸 주문 시도 수(다음 주문키 번호 계산용)."""
    return len(_pos_keys(pos_key, _fold()))


def is_locked(symbol: str, exclude_key: str | None = None) -> bool:
    """이 종목에 미해소 UNKNOWN 주문이 있나 — 있으면 신규/재주문 전면 금지.
    (대사 전 재주문 = 초과매도. 이 잠금이 마지막 방어선.)
    exclude_key: 지금 발주 중인 자기 주문키(그 키가 원인인 자기 차단 방지)."""
    with _file_lock(False):
        fold, corrupt = _fold_unlocked()
    if corrupt:
        return True
    for k, cur in fold.items():
        if k == exclude_key:
            continue
        if (cur.get("symbol") == symbol and cur.get("state") == "unknown"
                and not cur.get("reconciled")):
            return True
    return False


def locked_symbols() -> set:
    with _file_lock(False):
        fold, corrupt = _fold_unlocked()
    out = {c["symbol"] for c in fold.values()
           if c.get("state") == "unknown" and not c.get("reconciled")
           and c.get("symbol")}
    if corrupt:
        out.add("*")
    return out


def orders_for(symbol: str | None = None, *, side: str | None = None,
               key_prefix: str | None = None) -> list[dict]:
    """원장의 주문을 심볼·방향·키 접두사로 조회한다.

    보호 로직은 BUY 잔량과 SELL 잔량을 구분해야 한다. 종전의 종목 단위 개수만
    사용하면 미체결 BUY 하나 때문에 긴급 손절 SELL까지 멈추므로, 상태 조회를
    한곳에서 정규화한다.
    """
    want_symbol = str(symbol or "").upper() or None
    want_side = str(side or "").upper() or None
    out = []
    for key, cur in _fold().items():
        if want_symbol and str(cur.get("symbol") or "").upper() != want_symbol:
            continue
        if want_side and str(cur.get("side") or "").upper() != want_side:
            continue
        if key_prefix and not key.startswith(key_prefix):
            continue
        out.append({"key": key, **cur})
    return out


def open_orders(symbol: str | None = None, *, side: str | None = None) -> list:
    """종료되지 않은(잔여 가능성 있는) 주문 목록 — 대사·경합 판정 대상."""
    return [o for o in orders_for(symbol, side=side)
            if o.get("state") not in _TERMINAL
            and not (o.get("state") == "partial" and o.get("open") is False)]


# ── KIS 확장: 브로커 핸들(ODNO)·합성 대사키·신뢰도·동일종목 규칙 ─────────
#   KIS는 토스 clientOrderId 같은 클라 멱등키가 없다. 그래서:
#     · 주문응답의 ODNO(서버 채번)를 원장 key에 결속해 정정/취소·대사 핸들로 쓴다.
#     · 타임아웃으로 ODNO를 못 받으면, nccs/ccnl 행을 우리 주문과 대조할 합성키로 찾는다.
#     · 후보가 유일하면 HIGH(자동확정), 0/2+면 LOW(수동 잠금 유지) — 오매칭 초과매도 방지.

def bind_broker_order(key: str, odno: str, orgno: str = "",
                      ord_tmd: str = "") -> None:
    """주문응답의 ODNO(주문번호)·ORGNO(조직번호)·ORD_TMD(시각)를 원장 key에 결속.
    이 값이 있어야 나중에 정정/취소를 그 주문에 정확히 걸 수 있다."""
    _append({"ev": "bind", "key": key, "odno": str(odno),
             "orgno": str(orgno or ""), "ord_tmd": str(ord_tmd or "")})


def odno_of(key: str) -> str | None:
    return (_fold().get(key) or {}).get("odno")


def synthetic_key(account: str, excg: str, pdno: str, side: str,
                  qty, price, ord_tmd: str = "") -> str:
    """nccs/ccnl 행 ↔ 우리 주문을 대조할 결정적 합성키. 수량·단가는 반올림,
    ord_tmd는 분(HHMM) 버킷으로 근사(±윈도우). 같은 입력→같은 키(멱등)."""
    try:
        q = int(round(float(qty)))
    except (TypeError, ValueError):
        q = 0
    try:
        p = round(float(price), 2)
    except (TypeError, ValueError):
        p = 0.0
    tmin = str(ord_tmd or "")[:4]                 # HHMM(초 제거)
    raw = f"{account}|{excg}|{pdno}|{side}|{q}|{p}|{tmin}"
    return "syn-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def record_synthetic(key: str, synthetic: str) -> None:
    """제출 시 만든 합성키를 원장에 남긴다(타임아웃 대사에서 후보 귀속 근거)."""
    _append({"ev": "synthetic", "key": key, "synthetic": synthetic})


def reconcile_from_candidates(key: str, candidates: list,
                              intended: int | None = None) -> dict:
    """nccs/ccnl에서 이 주문으로 귀속시킨 후보들로 대사(신뢰도 판정, 리뷰 R3).

    candidates: [{"filled": int, ...}] — synthetic_key/ODNO로 이미 이 주문에 귀속된 행들.
    · 후보 정확히 1개  → HIGH: 그 체결량으로 확정(reconcile 호출 → 잠금 해제).
    · 후보 0 또는 2+   → LOW : 자동 해소 금지(잠금 유지). 사람이 판단(MANUAL_REVIEW_LOCK).
    반환: {state, filled, residual, confidence}. LOW면 state는 unknown 유지."""
    cur = state_of(key) or {}
    intended = cur.get("intended", 0) if intended is None else int(intended)
    # 후보가 브로커에 **아직 살아있는 주문**(nccs 잔량>0)이면 잠금 유지 —
    #   잔여를 재발주하면 원주문이 마저 체결돼 초과매도(감사 수정 #6). 완전 체결
    #   (ccnl 전용·open=False)만 자동 확정.
    if len(candidates) == 1 and candidates[0].get("open"):
        cand = candidates[0]
        seen = max(int(cur.get("filled", 0)),
                   int(cand.get("filled", 0) or 0))
        on_result(key, cur.get("state", "unknown"), seen,
                  fill_price=cand.get("price"),
                  fill_price_source=str(cand.get("src") or "broker"),
                  open_order=True)
        _append({"ev": "confidence", "key": key, "confidence": CONF_LOW,
                 "reason": "order_still_open"})
        return {"state": cur.get("state", "unknown"), "filled": seen,
                "residual": max(0, intended - seen), "confidence": CONF_LOW,
                "fill_price": cand.get("price"), "open": True}
    if len(candidates) == 1:
        cand = candidates[0]
        filled = max(0, int(cand.get("filled", 0) or 0))
        r = reconcile(key, filled, fill_price=cand.get("price"),
                      fill_price_source=str(cand.get("src") or "broker"),
                      open_order=False)           # 기존 대사 재사용(잠금 해제)
        _append({"ev": "confidence", "key": key, "confidence": CONF_HIGH})
        r["confidence"] = CONF_HIGH
        return r
    # 모호(0개 또는 2개+) → 잠금 유지. 자동 재주문 절대 금지.
    _append({"ev": "confidence", "key": key, "confidence": CONF_LOW,
             "candidates": len(candidates)})
    filled = int(cur.get("filled", 0))
    return {"state": cur.get("state", "unknown"), "filled": filled,
            "residual": max(0, intended - filled), "confidence": CONF_LOW}


def open_order_count(symbol: str, exclude_key: str | None = None,
                     *, side: str | None = None) -> int:
    """이 종목의 in-flight(결과 미확정: submitted/ack/unknown) 주문 수.
    동일종목 동시주문(오매칭·이중주문) 방지 판정용.
    exclude_key: 지금 발주 중인 자기 주문키는 세지 않는다(자기 차단 방지)."""
    want_side = str(side or "").upper() or None
    return sum(1 for k, v in _fold().items()
               if k != exclude_key and v.get("symbol") == symbol
               and (not want_side or str(v.get("side") or "").upper() == want_side)
               and v.get("state") in _INFLIGHT
               and not (v.get("state") == "partial" and v.get("open") is False))


def provisional_buy_protection(symbol: str) -> dict | None:
    """체결 대사 중인 BUY에서 임시 손절 메타를 복구한다.

    KIS 잔고에는 체결이 먼저 보이지만 kis_positions 기록은 다음 대사까지 늦을 수
    있다. 그 짧은 창에 보유를 '손절선 불명'으로 제외하지 않도록 주문 전 선기록된
    stop을 사용한다. 서로 다른 손절선 후보가 둘 이상이면 추측하지 않고 ambiguous를
    반환해 호출부가 수동 잠금을 걸게 한다.
    """
    candidates = []
    for o in orders_for(symbol, side="BUY"):
        state = str(o.get("state") or "")
        filled = int(o.get("filled") or 0)
        accounted = int(o.get("accounted") or 0)
        if state in _TERMINAL and not (filled > accounted):
            continue
        try:
            stop = float(o.get("stop") or 0)
        except (TypeError, ValueError):
            stop = 0.0
        if stop <= 0:
            continue
        candidates.append({**o, "stop": stop})
    if not candidates:
        return None
    stops = {round(float(o["stop"]), 8) for o in candidates}
    if len(stops) != 1:
        return {"ambiguous": True, "symbol": symbol,
                "keys": [o["key"] for o in candidates]}
    candidates.sort(key=lambda o: float(o.get("submitted_at")
                                        or o.get("created_at") or 0), reverse=True)
    return {**candidates[0], "ambiguous": False, "reconciling": True}


def last_submit_ts(symbol: str, exclude_key: str | None = None) -> float:
    fold = _fold()
    ts = [float(v.get("submitted_at", 0.0)) for k, v in fold.items()
          if k != exclude_key and v.get("symbol") == symbol]
    return max(ts) if ts else 0.0


def can_submit(symbol: str, min_interval_s: float = 60.0,
               now: float | None = None,
               exclude_key: str | None = None) -> bool:
    """R3 안전 게이트 — 다음이면 신규/추가 주문 금지(초과매도·오매칭 방지):
      · 종목이 UNKNOWN 잠금 상태거나
      · 이미 in-flight 주문이 있거나(동시 open order 1개 제한)
      · 직전 주문 후 min_interval_s(기본 60초) 이내.
    (Stage 2에서 동일 symbol/side 반복 주문의 대사 오매칭을 원천 차단.)
    exclude_key: 호출부가 이미 원장에 선기록한 '이번' 주문키 — 그 키가 자기 자신을
      in-flight/최근제출로 오인해 차단하는 것을 막는다(파수꾼 pre-record → place_order)."""
    now = time.time() if now is None else now
    with _file_lock(False):
        fold, corrupt = _fold_unlocked()
    if corrupt:
        return False
    return _can_submit_fold(
        fold, symbol, min_interval_s, float(now), exclude_key=exclude_key)
