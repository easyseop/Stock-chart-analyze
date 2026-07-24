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

Stage 0/모의에선 브로커가 '항상 체결(filled)'을 돌려주므로 이 기계는 지나가기만
하지만, 토스 실주문 붙일 때 UNKNOWN/부분체결/대사 경로가 이미 검증돼 있게 만든다.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

LEDGER_PATH = "bot/order_ledger.jsonl"

# 종료 상태(더 이상 잔여 없음) vs 진행 상태
_TERMINAL = {"filled", "rejected", "cancelled", "expired"}
# in-flight(결과 미확정) — 부분체결도 원주문 잔량이 살아 있으면 새 주문을 막아야 한다.
_INFLIGHT = {"submitted", "ack", "partial", "cancel_pending", "unknown"}

# 대사 신뢰도(리뷰 R3) — 후보가 유일하면 HIGH(자동확정), 모호하면 LOW(수동 잠금 유지)
CONF_HIGH = "high"
CONF_LOW = "low"


def _append(ev: dict) -> None:
    ev.setdefault("ts", time.time())
    os.makedirs(os.path.dirname(LEDGER_PATH) or ".", exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _fold() -> dict:
    """원장을 재생성 — key별 현재 상태로 접는다. {key: {...}}.
    filled는 '지금까지 확인된 총 체결량'(절대값). 손상된 줄은 건너뜀."""
    st: dict = {}
    try:
        with open(LEDGER_PATH, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                key = ev.get("key")
                if not key:
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
                              "chase", "ref_price", "reason"):
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
    return st


def record_submit(key: str, symbol: str, qty: int, reason: str = "",
                  meta: dict | None = None) -> None:
    """주문 전송 직전 기록. 반드시 send() 이전에 호출(크래시 대비)."""
    _append({"ev": "submit", "key": key, "symbol": symbol,
             "intended": int(qty), "filled": 0, "state": "submitted",
             "reason": reason, "meta": meta or {}})


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
    for k, cur in _fold().items():
        if k == exclude_key:
            continue
        if (cur.get("symbol") == symbol and cur.get("state") == "unknown"
                and not cur.get("reconciled")):
            return True
    return False


def locked_symbols() -> set:
    return {c["symbol"] for c in _fold().values()
            if c.get("state") == "unknown" and not c.get("reconciled")
            and c.get("symbol")}


def open_orders() -> list:
    """종료되지 않은(잔여 가능성 있는) 주문 목록 — 대사 대상."""
    return [{"key": k, **v} for k, v in _fold().items()
            if v.get("state") not in _TERMINAL
            and not (v.get("state") == "partial" and v.get("open") is False)]


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


def open_order_count(symbol: str, exclude_key: str | None = None) -> int:
    """이 종목의 in-flight(결과 미확정: submitted/ack/unknown) 주문 수.
    동일종목 동시주문(오매칭·이중주문) 방지 판정용.
    exclude_key: 지금 발주 중인 자기 주문키는 세지 않는다(자기 차단 방지)."""
    return sum(1 for k, v in _fold().items()
               if k != exclude_key and v.get("symbol") == symbol
               and v.get("state") in _INFLIGHT
               and not (v.get("state") == "partial" and v.get("open") is False))


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
    if is_locked(symbol, exclude_key=exclude_key):
        return False
    if open_order_count(symbol, exclude_key=exclude_key) >= 1:
        return False
    now = time.time() if now is None else now
    return (now - last_submit_ts(symbol, exclude_key=exclude_key)) >= min_interval_s
