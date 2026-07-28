"""KIS 봇 진입 포지션의 손절선 저장 — 파수꾼 브로커-진실 보호의 fallback stop 소스.

문제: 파수꾼은 autopaper feed의 손절선으로 보호하는데, 봇이 산 종목을 autopaper가
안 사면 feed에 손절선이 없다 → 그 KIS 보유가 무보호가 된다. 그래서 매수 루프가
진입할 때 **그 신호의 손절선을 여기 기록**해두고, 파수꾼이 feed에 없는 KIS 보유를
이 기록으로 보호한다(정적 손절 — 트레일링은 feed가 담당, 여기는 최소 방어선).

append-only JSONL(원장 철학) — 다중 프로세스(매수루프 write / 파수꾼 read) 안전.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import threading
import time

PATH = os.environ.get("KIS_POSITIONS_PATH",
                      os.path.join(os.path.dirname(__file__), "kis_positions.jsonl"))
_THREAD_LOCK = threading.RLock()


@contextmanager
def _file_lock(exclusive: bool):
    lock_path = PATH + ".lock"
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


def _append(ev: dict) -> None:
    """포지션/손절 메타를 프로세스 잠금+fsync로 영속한다."""
    ev.setdefault("ts", time.time())
    parent = os.path.dirname(PATH) or "."
    os.makedirs(parent, exist_ok=True)
    payload = (json.dumps(ev, ensure_ascii=False, separators=(",", ":"))
               + "\n").encode("utf-8")
    with _file_lock(True):
        existed = os.path.exists(PATH)
        fd = os.open(PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(PATH, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("kis_positions append made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed:
            dfd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)


def record(code: str, *, stop: float, ccy: str, entry: float | None = None,
           qty: int | None = None, name: str = "", opened: str = "",
           sleeve: str = "A", target: float | None = None,
           pos_key: str = "") -> None:
    """봇 진입 시 손절선 기록(매수 루프가 execute_entry 성공 후 호출).
    sleeve: 'A'/'B' — 슬리브별 예산·통계·청산 분리. target: B의 목표가(VAH)."""
    _append({"ev": "open", "code": str(code).upper(), "stop": float(stop),
             "ccy": ccy, "entry": entry, "qty": qty, "name": name,
             "opened": opened, "sleeve": sleeve, "target": target,
             "pos_key": pos_key})


def apply_buy_fill(code: str, *, qty: int, price: float, stop: float,
                   ccy: str, pos_key: str, name: str = "", opened: str = "",
                   sleeve: str = "A", target: float | None = None,
                   event_id: str = "") -> None:
    """확정된 매수 체결만 포지션에 반영한다. ack 단계에서는 호출하지 않는다."""
    _append({"ev": "buy_fill", "code": str(code).upper(), "qty": int(qty),
             "price": float(price), "stop": float(stop), "ccy": ccy,
             "pos_key": pos_key, "name": name, "opened": opened,
             "sleeve": sleeve, "target": target,
             "event_id": str(event_id or "")})


def apply_sell_fill(code: str, *, qty: int, price: float,
                    pos_key: str = "", event_id: str = "") -> None:
    """확정된 매도 체결 수량만 차감한다. 잔량 0이면 fold에서 자동 소멸한다."""
    _append({"ev": "sell_fill", "code": str(code).upper(), "qty": int(qty),
             "price": float(price), "pos_key": pos_key,
             "event_id": str(event_id or "")})


def migrate_legacy(code: str, *, qty: int, entry: float, stop: float,
                   stop0: float, ccy: str, pos_key: str, name: str = "",
                   opened: str = "", sleeve: str = "A",
                   target: float | None = None, event_id: str = "") -> None:
    """구버전 포지션을 정확한 수량·정체성으로 1회 교체한다.

    일반 매수 체결의 증가(delta) 이벤트와 달리 ``qty``를 절대값으로 설정한다.
    legacy 이관 전용이며 event_id 멱등성을 반드시 사용한다.
    """
    if not event_id:
        raise ValueError("legacy migration requires event_id")
    if int(qty) <= 0 or float(entry) <= 0 or float(stop) <= 0 \
            or float(stop0) <= 0 or not str(pos_key).strip():
        raise ValueError("invalid legacy migration position")
    _append({
        "ev": "legacy_migrate", "code": str(code).upper(),
        "qty": int(qty), "entry": float(entry), "stop": float(stop),
        "stop0": float(stop0), "ccy": str(ccy), "pos_key": str(pos_key),
        "name": str(name), "opened": str(opened),
        "sleeve": str(sleeve).upper(), "target": target,
        "legacy_migrated": True, "event_id": str(event_id),
    })


def close(code: str) -> None:
    """전량 청산 확정 시 기록(더는 보호 불필요 — 폴드에서 제거)."""
    _append({"ev": "close", "code": str(code).upper()})


def raise_stop(code: str, stop: float) -> None:
    """손절선 상향 래칫(본전·트레일) — 올리기만, 내리기 이벤트는 없다.
    KIS 청산관리자(kis_exits)가 기록 → 파수꾼 fallback 손절선에 즉시 반영."""
    _append({"ev": "raise", "code": str(code).upper(), "stop": float(stop)})


def mark_half_done(code: str, *, event_id: str) -> None:
    """1주 half_done까지 포함해 +1R 절반익절 완료 사실을 원장에 영속한다."""
    if not str(event_id or "").strip():
        raise ValueError("half_done event_id 필요")
    _append({
        "ev": "half_done", "code": str(code).upper(),
        "event_id": str(event_id),
    })


def load() -> dict:
    """{code: {stop, ccy, entry, qty, name, opened}} — 열린 것만. 최신 open 우선."""
    st: dict = {}
    seen_events: set[str] = set()
    try:
        with _file_lock(False):
            with open(PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    event_id = str(ev.get("event_id") or "")
                    if event_id and event_id in seen_events:
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
                                    "sleeve": ev.get("sleeve", "A"),
                                    "target": ev.get("target"),
                                    "pos_key": ev.get("pos_key", "")}
                    elif ev.get("ev") == "buy_fill":
                        q = max(0, int(ev.get("qty") or 0))
                        px = float(ev.get("price") or 0)
                        if q <= 0 or px <= 0:
                            continue
                        if code not in st:
                            stop = float(ev.get("stop") or 0)
                            st[code] = {"code": code, "stop": stop, "stop0": stop,
                                        "ccy": ev.get("ccy"), "entry": px, "qty": q,
                                        "name": ev.get("name", ""),
                                        "opened": ev.get("opened", ""),
                                        "sleeve": ev.get("sleeve", "A"),
                                        "target": ev.get("target"),
                                        "pos_key": ev.get("pos_key", "")}
                        else:
                            cur = st[code]
                            old_q = max(0, int(cur.get("qty") or 0))
                            old_px = float(cur.get("entry") or 0)
                            cur["entry"] = (
                                (old_px * old_q + px * q) / (old_q + q)
                                if old_q + q > 0 else px)
                            cur["qty"] = old_q + q
                    elif ev.get("ev") == "legacy_migrate":
                        q = max(0, int(ev.get("qty") or 0))
                        entry = float(ev.get("entry") or 0)
                        stop = float(ev.get("stop") or 0)
                        stop0 = float(ev.get("stop0") or stop)
                        if q <= 0 or entry <= 0 or stop <= 0 or stop0 <= 0:
                            continue
                        st[code] = {
                            "code": code, "stop": stop, "stop0": stop0,
                            "ccy": ev.get("ccy"), "entry": entry, "qty": q,
                            "name": ev.get("name", ""),
                            "opened": ev.get("opened", ""),
                            "sleeve": ev.get("sleeve", "A"),
                            "target": ev.get("target"),
                            "pos_key": ev.get("pos_key", ""),
                            "legacy_migrated": True,
                        }
                    elif ev.get("ev") == "sell_fill" and code in st:
                        st[code]["qty"] = max(
                            0, int(st[code].get("qty") or 0)
                            - int(ev.get("qty") or 0))
                        if st[code]["qty"] <= 0:
                            st.pop(code, None)
                    elif ev.get("ev") == "raise" and code in st:
                        try:                        # 래칫: 올리기만(내림 무시)
                            new = float(ev.get("stop") or 0)
                            if new > float(st[code].get("stop") or 0):
                                st[code]["stop"] = new
                        except (TypeError, ValueError):
                            pass
                    elif ev.get("ev") == "half_done" and code in st:
                        st[code]["half_done"] = True
                    if event_id:
                        seen_events.add(event_id)
    except FileNotFoundError:
        pass
    return st
