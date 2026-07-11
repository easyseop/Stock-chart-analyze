"""원가장부(IS3 원가축 + C 정산회계 최소구현) — lot 단위 cost_krw 회계.

봉투(envelope)의 분모·게이트가 요구하는 값을 공급한다:
  · bot_open_cost        = Σ(열린 lot cost_krw)      → 총량 게이트 분자
  · open_cost(symbol)    = 종목별 열린 원가           → symbol_cap 차감
  · totals()             = Σ매수원가·Σ매도실현액       → bot_cash 계산

규칙(IS3):
  · 매수 확정 체결 시 add_lot(cost_krw = fill_price × qty × fx + commission) —
    **fill 시점 fx로 원가 고정**(이후 환율은 평가액만 바꿈, footprint 불변).
  · 매도 확정 체결 시 close_lot(proceeds_krw = 실현액 − 수수료·세금) —
    cost 반환이 아니라 **proceeds 환입**(손실 후 과대계상 방지).
  · append-only JSONL(원장과 동일 철학) — 재시작 시 fold로 재구성, 크래시 안전.
  · 부분 청산은 lot 원가를 수량 비례로 차감.

호출 시점: 체결이 '확정'됐을 때만(ccnl 대사·reconcile HIGH·fill 폴링) —
ack(접수)에서 기록 금지(미체결 주문을 원가로 잡으면 예산 과소·이중계상 위험.
단, UNKNOWN 구간의 보수적 예약 차감은 envelope 호출부(X1)가 intended로 처리).
"""
from __future__ import annotations

import json
import os

BOOK_PATH = os.path.join(os.path.dirname(__file__), "costbook.jsonl")


def _append(ev: dict) -> None:
    path = os.environ.get("COSTBOOK_PATH", BOOK_PATH)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _fold() -> dict:
    """{pos_key: {symbol, qty, cost_krw}} — 열린 lot들. 닫힌 lot은 qty 0."""
    path = os.environ.get("COSTBOOK_PATH", BOOK_PATH)
    lots: dict = {}
    totals = {"buy_cost": 0.0, "sell_proceeds": 0.0}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                k = ev.get("key")
                if ev.get("ev") == "add":
                    cur = lots.setdefault(k, {"symbol": ev.get("symbol"),
                                              "qty": 0, "cost_krw": 0.0})
                    cur["qty"] += int(ev.get("qty", 0))
                    cur["cost_krw"] += float(ev.get("cost_krw", 0.0))
                    totals["buy_cost"] += float(ev.get("cost_krw", 0.0))
                elif ev.get("ev") == "close" and k in lots:
                    cur = lots[k]
                    q = min(int(ev.get("qty", 0)), cur["qty"])
                    if cur["qty"] > 0:
                        cur["cost_krw"] -= cur["cost_krw"] * (q / cur["qty"])
                    cur["qty"] -= q
                    totals["sell_proceeds"] += float(ev.get("proceeds_krw", 0.0))
    except FileNotFoundError:
        pass
    return {"lots": lots, "totals": totals}


def add_lot(pos_key: str, symbol: str, qty: int, fill_price: float,
            fx: float = 1.0, commission_krw: float = 0.0) -> float:
    """매수 확정 체결 기록. 반환: 이번 lot의 cost_krw(fx로 고정)."""
    cost = float(fill_price) * int(qty) * float(fx) + float(commission_krw)
    _append({"ev": "add", "key": pos_key, "symbol": symbol, "qty": int(qty),
             "cost_krw": cost, "fill_price": float(fill_price), "fx": float(fx),
             "commission_krw": float(commission_krw)})
    return cost


def close_lot(pos_key: str, qty: int, proceeds_krw: float) -> None:
    """매도 확정 체결 기록 — proceeds(수수료·세금 차감 후 실현액) 환입."""
    _append({"ev": "close", "key": pos_key, "qty": int(qty),
             "proceeds_krw": float(proceeds_krw)})


def open_cost_total() -> float:
    return sum(l["cost_krw"] for l in _fold()["lots"].values() if l["qty"] > 0)


def open_cost_symbol(symbol: str) -> float:
    return sum(l["cost_krw"] for l in _fold()["lots"].values()
               if l["qty"] > 0 and l.get("symbol") == symbol)


def open_qty(symbol: str) -> int:
    """봇 claim 수량(원장 관점) — IS5 비대칭 대사·매도 상한 입력."""
    return sum(l["qty"] for l in _fold()["lots"].values()
               if l.get("symbol") == symbol)


def totals() -> dict:
    """{buy_cost, sell_proceeds} — envelope.bot_cash 입력."""
    return dict(_fold()["totals"])
