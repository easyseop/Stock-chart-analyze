"""계좌 단위 총 open risk 상한 — '동시에 다 맞으면 얼마 잃나'를 제한한다.

배경(외부 검토 2026-08-19 P0): 거래당 1%·종목당 1/3 상한은 있지만 **합산**
계획손실을 제한하는 규칙이 없었다. 1% 포지션이 20개면 동시 손절 시 20%다.
각 매매는 전부 규칙을 지켰는데 계좌는 하루에 크게 잃을 수 있는 구조.

정의: 총 open risk = Σ max(0, entry − stop) × qty  (원장 kis_positions 기준,
USD는 환율 환산). 손절이 진입가 위로 래칫된 포지션(이익 보호)은 0으로 센다 —
지금 손절이 다 맞아도 그 포지션은 잃지 않기 때문.

fail-closed 계약:
  · stop·entry·qty를 계량할 수 없는 행(무보호·손상)은 '위험 불명'으로 분류하고
    **신규 매수를 차단**한다. 위험을 모르는 채 위험을 더하는 것이 최악이다
    (실측 2026-08-18: CVNA 74주가 원장 밖에서 무보호로 발견됨 — 단, 원장 밖
    고아는 이 게이트가 보지 못한다. scripts/kis_orphan_audit.py가 그 짝이다).
  · 원장 읽기 실패도 차단(조회 실패 ≠ 위험 0).

한계(문서화): 접수됐지만 미체결인 주문의 예약 위험은 아직 세지 않는다.
분모는 envelope.operating_total_krw()(A+B 시드 합) — 예산 게이트와 같은 축.
"""
from __future__ import annotations

import math
import os

DEFAULT_MAX_OPEN_RISK_FRACTION = 0.10   # 시드 대비 10% — 첫 배포는 관측 후 조정


def max_fraction() -> float:
    raw = os.environ.get("MAX_OPEN_RISK_FRACTION",
                         DEFAULT_MAX_OPEN_RISK_FRACTION)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_OPEN_RISK_FRACTION
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_MAX_OPEN_RISK_FRACTION
    # 1 초과는 1로 클램프 — 완화하려는 설정을 조용히 기본값(더 엄격)으로
    #   되돌리면 안 된다(self-heal P2-1과 같은 방향 결함 방지).
    return min(value, 1.0)


def _num(value) -> float | None:
    """양의 유한 숫자만. bool·NaN·inf·문자·None은 None(계량 불가)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out > 0 else None


def open_risk(positions: dict, fx: float) -> dict:
    """원장 포지션의 합산 계획손실(KRW)과 계량 불가 목록.

    반환 {"defined_krw", "rows": [{code, risk_krw}], "unknown": [codes]}.
    """
    defined = 0.0
    rows: list[dict] = []
    unknown: list[str] = []
    for code, row in sorted((positions or {}).items()):
        if not isinstance(row, dict):
            unknown.append(str(code)); continue
        qty = row.get("qty")
        if isinstance(qty, bool) or qty is None:
            unknown.append(str(code)); continue
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            unknown.append(str(code)); continue
        if qty <= 0:
            continue                       # 청산 중/빈 행 — 위험 없음
        entry = _num(row.get("entry"))
        stop = _num(row.get("stop"))
        if entry is None or stop is None:
            unknown.append(str(code)); continue
        ccy = str(row.get("ccy") or "").upper()
        if ccy not in ("KRW", "USD"):
            # ccy 유실 행에 환율을 곱하면 KR 종목이 1380배 과대평가돼 사실상
            #   영구 차단이 된다. 계량 불가로 정직하게 분류한다(차단은 동일).
            unknown.append(str(code)); continue
        per_share = max(0.0, entry - stop)
        risk = per_share * qty
        if ccy == "USD":
            risk *= fx
        defined += risk
        if risk > 0:
            rows.append({"code": str(code), "risk_krw": round(risk, 2)})
    rows.sort(key=lambda r: -r["risk_krw"])
    return {"defined_krw": round(defined, 2), "rows": rows, "unknown": unknown}


def gate(fx: float) -> tuple[bool, str, dict]:
    """신규 매수 허용 여부. (ok, why, snapshot). 실패는 전부 차단 방향."""
    from bot import envelope, kis_positions
    try:
        positions = kis_positions.load()
    except Exception as exc:
        return False, f"원장 읽기 실패({type(exc).__name__}) — 위험 총량 불명", {}
    snap = open_risk(positions, fx)
    if snap["unknown"]:
        return (False,
                f"위험 계량 불가 포지션 {len(snap['unknown'])}건"
                f"({','.join(snap['unknown'][:5])}) — 신규 매수 차단", snap)
    seed = float(envelope.operating_total_krw() or 0)
    if seed <= 0:
        # 시드 자체의 검증은 execute_entry 사이징 소관(미설정=수량 0)이다.
        # 여기서는 비율만 판정한다 — 위험이 0이면 분모 없이도 비율은 0이므로
        # 허용하고, 위험이 있는데 분모가 없으면 계산 불가로 차단한다.
        if snap["defined_krw"] <= 0:
            return True, "총 open risk 0 — 게이트 통과", snap
        return False, "시드 불명(operating_total<=0)인데 open risk 존재 — 차단", snap
    frac = snap["defined_krw"] / seed
    cap = max_fraction()
    snap.update({"seed_krw": round(seed, 2), "fraction": round(frac, 4),
                 "cap": cap})
    if frac >= cap:
        return (False,
                f"총 open risk {frac * 100:.1f}% ≥ 상한 {cap * 100:.0f}%"
                f" ({snap['defined_krw']:,.0f}/{seed:,.0f}원)", snap)
    return True, f"총 open risk {frac * 100:.1f}% < {cap * 100:.0f}%", snap
