"""KIS 청산(익절) 관리자 — 전략의 매도 규칙을 KIS 계좌에 직접 집행.

정합성 점검(2026-07-24) 반영: 파수꾼은 손절만 집행해 KIS가 '이익 실현 없는
반쪽 전략'으로 돌았다(웹 모의와 보유 종목이 달라 매도 미러링만으론 커버 불가).
→ 봇 보유 전 종목에 전략 A의 청산 프로파일을 R단위로 집행:
  ① +1R 도달: 절반 익절 + 손절선 본전(진입가) 래칫
  ② 이후 트레일: 손절선 = max(현재, 최고가 − 1.5R)  (원전략 3×ATR ≈ 1.5R 근사)
  ③ 21일 타임스탑: +1R 미도달이면 전량 정리
  실제 '팔기'는 손절선 터치 시 파수꾼이 집행(래칫된 선) — 이 모듈은 선을 올리고
  절반익절·타임스탑만 직접 주문한다. 손절 로직과 충돌 없음.

호출: 파수꾼 check_once 말미(20초 주기·장중만). 실패는 사이클에 무해.
상태: bot/kis_exits_state.json {code: {half, high}} — 포지션 소멸 시 자동 정리.
"""
from __future__ import annotations

import datetime
import json
import os

from bot import kis_positions, ledger, notify

STATE_PATH = os.environ.get(
    "KIS_EXITS_STATE", os.path.join(os.path.dirname(__file__), "kis_exits_state.json"))
TRAIL_R = 1.5            # 트레일 폭(R) — 원전략 3×ATR(≈1.5R)과 동일 스케일
TIME_STOP_DAYS = 21      # autopaper.TIME_STOP_DAYS와 동일


def _load() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(st: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def decide(entry: float, stop0: float, cur_stop: float, price: float,
           qty: int, xs: dict, opened: str, today: str) -> list[tuple]:
    """청산 판단(순수 함수) — 영구 상태(half)는 바꾸지 않는다(코덱스 P0 반영:
    주문 성공 전 half=True 저장 시 매도 실패가 영구 누락되던 버그). 반환 액션:
      ("half_sell", 수량)  — +1R 절반 매도 제안(성공 시 호출부가 half 확정)
      ("half_done",)       — 1주라 매도 없이 래칫만(호출부가 half 확정)
      ("raise", 값)        — 손절선 상향
      ("sell", 수량, 사유) — 전량 정리(타임스탑)
    xs: {"half": bool, "high": float} — high(최고가)만 여기서 갱신(정보성).
    """
    acts: list[tuple] = []
    if entry <= 0 or stop0 <= 0 or price <= 0 or qty <= 0 or entry <= stop0:
        return acts
    R = entry - stop0
    xs["high"] = max(float(xs.get("high") or 0.0), price)

    # ① +1R 절반 익절 제안 — half 확정은 주문 성공 후 호출부가
    if not xs.get("half") and price >= entry + R:
        if qty >= 2:
            acts.append(("half_sell", qty // 2))
        else:
            acts.append(("half_done",))
    # ② 트레일(절반익절 확정 후) — 최고가 − 1.5R, 올리기만
    if xs.get("half"):
        trail = xs["high"] - TRAIL_R * R
        if trail > cur_stop:
            acts.append(("raise", trail))
    # ③ 타임스탑 — 21일 지나도 +1R 미도달이면 전량 정리
    try:
        d0 = datetime.date.fromisoformat(str(opened))
        dn = datetime.date.fromisoformat(str(today))
        if not xs.get("half") and (dn - d0).days >= TIME_STOP_DAYS \
                and price < entry + R:
            acts.append(("sell", qty, f"타임스탑 {TIME_STOP_DAYS}일"))
    except (ValueError, TypeError):
        pass                                   # 개시일 불명 — 타임스탑 생략
    return acts


def decide_b(target: float, price: float, qty: int,
             opened: str, today: str) -> list[tuple]:
    """슬리브 B 전용 청산(코덱스 P1 반영) — A 규칙(+1R/트레일) 대신:
      · 목표(VAH) 도달 → 전량 익절   · 타임스탑 21일 → 전량 정리
    손절(반등저점 아래)은 파수꾼이 기존 경로로 집행."""
    acts: list[tuple] = []
    if qty <= 0 or price <= 0:
        return acts
    if target and price >= target:
        acts.append(("sell", qty, "B 목표(VAH) 도달"))
        return acts
    try:
        d0 = datetime.date.fromisoformat(str(opened))
        dn = datetime.date.fromisoformat(str(today))
        if (dn - d0).days >= TIME_STOP_DAYS:
            acts.append(("sell", qty, f"B 타임스탑 {TIME_STOP_DAYS}일"))
    except (ValueError, TypeError):
        pass
    return acts


def manage(broker, held: dict, today: str) -> None:
    """봇 보유(진입 기록 있는 종목)에 청산 규칙 적용. 파수꾼이 매 사이클 호출."""
    kpos = kis_positions.load()
    st = _load()
    changed = False
    for code, p in list(held.items()):
        rec = kpos.get(code)
        if not rec or not rec.get("entry"):
            continue                            # 봇 진입 기록 없음(기보유 등) — 제외
        if ledger.open_order_count(code) >= 1:
            continue                            # in-flight 있으면 건드리지 않음
        price = broker.quote(code, p.get("ccy", "USD"))
        if not price or price <= 0:
            continue
        xs = st.setdefault(code, {"half": False, "high": 0.0})
        entry = float(rec["entry"])
        stop0 = float(rec.get("stop0") or rec.get("stop") or 0)
        cur_stop = float(p.get("stop") or rec.get("stop") or 0)
        qty = int(p.get("q") or 0)
        if rec.get("sleeve") == "B":               # B 전용 청산(목표 VAH·타임스탑)
            acts = decide_b(float(rec.get("target") or 0), price, qty,
                            rec.get("opened", ""), today)
        else:
            acts = decide(entry, stop0, cur_stop, price, qty, xs,
                          rec.get("opened", ""), today)
        for act in acts:
            changed = True
            if act[0] == "raise":
                kis_positions.raise_stop(code, float(act[1]))
                notify.send(f"🔒 <b>손절선 상향</b> — {code} → {act[1]:.2f} "
                            f"(이익 보호 래칫)")
            elif act[0] == "half_done":            # 1주 — 매도 없이 본전 래칫만
                xs["half"] = True
                if entry > cur_stop:
                    kis_positions.raise_stop(code, entry)
                    notify.send(f"🔒 <b>손절선 본전</b> — {code} (+1R 도달, 1주라 홀드)")
            elif act[0] == "half_sell":
                sq = int(act[1])
                key = f"xe:{code}:half:{rec.get('opened','')}"
                r = broker.place_sell(code, sq, "익절 +1R 절반", key)
                ok = bool(r) if not isinstance(r, dict) else r.get("state") in ("ack", "filled")
                if ok:                             # 성공 후에만 half 확정(P0 수정)
                    xs["half"] = True
                    if entry > cur_stop:
                        kis_positions.raise_stop(code, entry)
                notify.send(f"💰 <b>KIS 익절 +1R 절반</b> — {code} {sq}주 매도 "
                            f"{'접수 · 손절선 본전으로' if ok else '실패 — 다음 사이클 재시도'}",
                            critical=True)
            elif act[0] == "sell":
                _, sq, why = act
                key = f"xe:{code}:{'btgt' if '목표' in why else 'time'}:{rec.get('opened','')}"
                r = broker.place_sell(code, int(sq), why, key)
                ok = bool(r) if not isinstance(r, dict) else r.get("state") in ("ack", "filled")
                notify.send(f"💰 <b>KIS {why}</b> — {code} {sq}주 매도 "
                            f"{'접수' if ok else '실패 — 다음 사이클 재시도'}",
                            critical=True)
        st[code] = xs
    #  소멸 포지션 상태 정리
    for code in list(st):
        if code not in held:
            st.pop(code, None)
            changed = True
    if changed or st:
        _save(st)
