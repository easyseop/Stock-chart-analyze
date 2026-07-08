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

import json
import os

LEDGER_PATH = "bot/order_ledger.jsonl"

# 종료 상태(더 이상 잔여 없음) vs 진행 상태
_TERMINAL = {"filled", "rejected"}


def _append(ev: dict) -> None:
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
    except FileNotFoundError:
        pass
    return st


def record_submit(key: str, symbol: str, qty: int, reason: str = "",
                  meta: dict | None = None) -> None:
    """주문 전송 직전 기록. 반드시 send() 이전에 호출(크래시 대비)."""
    _append({"ev": "submit", "key": key, "symbol": symbol,
             "intended": int(qty), "filled": 0, "state": "submitted",
             "reason": reason, "meta": meta or {}})


def on_result(key: str, state: str, filled_qty: int = 0) -> None:
    """전송 결과 반영. state ∈ submitted/ack/partial/filled/rejected/unknown.
    filled_qty는 '지금까지 확인된 총 체결량'(절대값)."""
    _append({"ev": "result", "key": key, "state": state,
             "filled": int(filled_qty)})


def reconcile(key: str, actual_filled: int) -> dict:
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
    _append({"ev": "reconcile", "key": key, "state": state, "filled": actual})
    return {"state": state, "filled": actual,
            "residual": max(0, intended - actual)}


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


def is_locked(symbol: str) -> bool:
    """이 종목에 미해소 UNKNOWN 주문이 있나 — 있으면 신규/재주문 전면 금지.
    (대사 전 재주문 = 초과매도. 이 잠금이 마지막 방어선.)"""
    for cur in _fold().values():
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
            if v.get("state") not in _TERMINAL]
