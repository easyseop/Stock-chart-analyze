"""KIS 봇 진입 포지션의 손절선 저장 — 파수꾼 브로커-진실 보호의 fallback stop 소스.

문제: 파수꾼은 autopaper feed의 손절선으로 보호하는데, 봇이 산 종목을 autopaper가
안 사면 feed에 손절선이 없다 → 그 KIS 보유가 무보호가 된다. 그래서 매수 루프가
진입할 때 **그 신호의 손절선을 여기 기록**해두고, 파수꾼이 feed에 없는 KIS 보유를
이 기록으로 보호한다(정적 손절 — 트레일링은 feed가 담당, 여기는 최소 방어선).

append-only JSONL(원장 철학) — 다중 프로세스(매수루프 write / 파수꾼 read) 안전.
"""
from __future__ import annotations

import json
import os
import time

PATH = os.environ.get("KIS_POSITIONS_PATH",
                      os.path.join(os.path.dirname(__file__), "kis_positions.jsonl"))


def _append(ev: dict) -> None:
    ev.setdefault("ts", time.time())
    os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
    with open(PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def record(code: str, *, stop: float, ccy: str, entry: float | None = None,
           qty: int | None = None, name: str = "", opened: str = "",
           sleeve: str = "A") -> None:
    """봇 진입 시 손절선 기록(매수 루프가 execute_entry 성공 후 호출).
    sleeve: 'A'(전환확정) / 'B'(매물대 반등) — 슬리브별 예산·통계 분리용."""
    _append({"ev": "open", "code": str(code).upper(), "stop": float(stop),
             "ccy": ccy, "entry": entry, "qty": qty, "name": name,
             "opened": opened, "sleeve": sleeve})


def close(code: str) -> None:
    """전량 청산 확정 시 기록(더는 보호 불필요 — 폴드에서 제거)."""
    _append({"ev": "close", "code": str(code).upper()})


def raise_stop(code: str, stop: float) -> None:
    """손절선 상향 래칫(본전·트레일) — 올리기만, 내리기 이벤트는 없다.
    KIS 청산관리자(kis_exits)가 기록 → 파수꾼 fallback 손절선에 즉시 반영."""
    _append({"ev": "raise", "code": str(code).upper(), "stop": float(stop)})


def load() -> dict:
    """{code: {stop, ccy, entry, qty, name, opened}} — 열린 것만. 최신 open 우선."""
    st: dict = {}
    try:
        with open(PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                code = str(ev.get("code") or "").upper()
                if not code:
                    continue
                if ev.get("ev") == "close":
                    st.pop(code, None)
                elif ev.get("ev") == "open":
                    st[code] = {"code": code, "stop": ev.get("stop"),
                                "stop0": ev.get("stop"),   # 진입 손절(R 계산 기준, 래칫 불변)
                                "ccy": ev.get("ccy"), "entry": ev.get("entry"),
                                "qty": ev.get("qty"), "name": ev.get("name", ""),
                                "opened": ev.get("opened", ""),
                                "sleeve": ev.get("sleeve", "A")}
                elif ev.get("ev") == "raise" and code in st:
                    try:                        # 래칫: 올리기만(내림 무시)
                        new = float(ev.get("stop") or 0)
                        if new > float(st[code].get("stop") or 0):
                            st[code]["stop"] = new
                    except (TypeError, ValueError):
                        pass
    except FileNotFoundError:
        pass
    return st
