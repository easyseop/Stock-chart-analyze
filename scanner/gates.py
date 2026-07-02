"""추천 게이트 — "이 종목을 추천해도 되는가"의 단일 출처(single source of truth).

이 세션에서 사용자 검토로 쌓인 규칙을 전부 한곳에 모은다. 과거엔 analyze/plan/
screener가 같은 질문("이미 올랐나", "추천해도 되나")을 각자 다른 임계값으로 판정해
우회 구멍(NXPI가 🎯 경로로 통과)·모순(진입=현재가인데 눌림 대기)이 반복됐다.

설계 원칙:
  1) 게이트는 '순차 통과' — 하나라도 걸리면 탈락. OR 입구(여러 진입로) 금지.
  2) 모든 임계값은 config에서만 온다(하드코딩 금지).
  3) audit()가 생성 결과의 불변식을 재검사 — 위반이면 빌드를 실패시켜
     나쁜 추천이 배포되는 일 자체를 막는다.

사용자 핵심 원칙(추천 방향):
  하락→상승 '전환 후보'만, 저점에서. 이미 상승추세(정배열-only)·이미 폭등·
  동전주/부실주·잡주(저유동)·손절폭 과대는 추천하지 않는다.
"""
from __future__ import annotations

import config
from scanner import plan


# ── 하드 제외(모든 추천 공통: 지금진입·관찰·⭐ 전부) ─────────────────

def exclusion_reasons(r: dict) -> list[str]:
    """추천 부적합 사유 목록. 빈 리스트 = 통과. 사유는 로그/디버깅용 한국어."""
    reasons = []
    if r.get("vetoed") or r.get("entry_kind") == "avoid":
        reasons.append("하락추세/방어선 이탈")
    if plan.junk(r):
        reasons.append("동전주·심한 부실")
    ccy = r.get("ccy", "USD")
    turn = r.get("turnover", 0) or 0
    liq_min = config.LIQ_MIN_KRW if ccy == "KRW" else config.LIQ_MIN_USD
    if turn < liq_min:
        reasons.append("저유동성(잡주)")
    ext = r.get("ext") or {}
    rs_rel = (r.get("rs") or {}).get("rel") or 0
    if (rs_rel >= config.BLOWOFF_RATIO
            or ext.get("ma120_stretch", 0) >= config.BLOWOFF_RATIO):
        reasons.append("이미 폭등")
    return reasons


def _stop_pct(r: dict) -> float:
    entry = r.get("entry") or 0
    stop = (r.get("risk") or {}).get("stop") or 0
    return (entry - stop) / entry if entry else 0.0


# ── 추천 분류(단일 진입점) ────────────────────────────────────────────

def classify(r: dict) -> dict:
    """{'group': 'now'|'watch'|None, 'reasons': [...]} — 추천 여부·그룹 판정.

    • now   = 전환 확정/대기(③·④)이고 지금이 살 자리(지지 근처, 과열·추격 아님).
    • watch = 전환 임박·갓돌파(①·②) 또는 ③·④인데 눌림/돌파 대기.
    • None  = 추천 안 함(사유 reasons에).
    """
    reasons = exclusion_reasons(r)
    if reasons:
        return {"group": None, "reasons": reasons}

    th = plan.thesis(r)
    stage = r.get("transition_stage", 0)
    kind = r.get("entry_kind", "now")
    ext = r.get("ext") or {}
    entry = r.get("entry") or 0
    price = (r.get("sr") or {}).get("price") or 0
    stop_pct = _stop_pct(r)
    already_ran = ext.get("runup63", 0) >= config.RECENT_RUNUP_MAX
    far_pull = (kind == "pullback" and price and entry
                and (price - entry) / price >= config.MAX_PULLBACK_GAP)

    if th["now"] and stage >= 3:
        if stop_pct >= config.MAX_STOP_NOW:
            return {"group": None, "reasons": ["손절폭 과대(지금진입)"]}
        if already_ran:
            return {"group": None, "reasons": ["최근 3개월 급등(고점 추격)"]}
        return {"group": "now", "reasons": []}

    if stage in (1, 2) or (stage >= 3 and kind in ("pullback", "breakout")):
        if stop_pct >= config.MAX_STOP_WATCH:
            return {"group": None, "reasons": ["손절폭 과대(관찰)"]}
        if far_pull:
            return {"group": None, "reasons": ["눌림 목표 과도(대폭락 대기)"]}
        return {"group": "watch", "reasons": []}

    return {"group": None, "reasons": ["전환 후보 아님(정배열-only 등)"]}


# ── 자가검증(불변식) — 빌드 때 실행, 위반 시 배포 차단 ────────────────

def audit(results: list[dict], picks: dict) -> None:
    """생성된 분석·추천의 불변식 재검사. 위반이 있으면 RuntimeError.

    screener.build()가 파일을 쓰기 전에 호출 → 위반 빌드는 실패해 배포되지 않는다.
    (지난 회귀: NXPI 우회, KRC 정배열 추천, GPUS 동전주, AES 손절 −1%, DB하이텍 −23%)
    """
    bad = []
    tol = 1e-6

    for r in results:
        code = r.get("code", "?")
        entry = r.get("entry") or 0
        price = (r.get("sr") or {}).get("price") or 0
        risk = r.get("risk") or {}
        stop, target = risk.get("stop"), risk.get("target")
        kind = r.get("entry_kind", "now")
        if entry and stop is not None and not stop < entry:
            bad.append(f"{code}: 손절({stop:.4g}) ≥ 진입({entry:.4g})")
        if entry and target is not None and not target > entry:
            bad.append(f"{code}: 목표({target:.4g}) ≤ 진입({entry:.4g})")
        if kind == "pullback" and entry and price and entry > price * (1 + tol):
            bad.append(f"{code}: 눌림 진입({entry:.4g})이 현재가({price:.4g}) 위")
        if kind in ("now", "wait", "avoid") and entry and price \
                and abs(entry - price) > price * 1e-4:
            bad.append(f"{code}: kind={kind}인데 진입≠현재가")

    rmap = {r["code"]: r for r in results}
    seen = set()
    for grp in ("now", "watch"):
        for p in picks.get(grp, []):
            code = p.get("code")
            if code in seen:
                bad.append(f"{code}: 추천 중복")
            seen.add(code)
            r = rmap.get(code)
            if r is None:
                bad.append(f"{code}: 추천에 있는데 분석 결과 없음")
                continue
            why = exclusion_reasons(r)
            if why:
                bad.append(f"{code}: 하드 제외 대상인데 추천됨({','.join(why)})")
            sp = _stop_pct(r)
            cap = config.MAX_STOP_NOW if grp == "now" else config.MAX_STOP_WATCH
            if sp >= cap + tol:
                bad.append(f"{code}: {grp} 손절폭 {sp*100:.0f}% ≥ {cap*100:.0f}%")
            if grp == "now":
                if r.get("transition_stage", 0) < 3:
                    bad.append(f"{code}: now인데 전환단계 <3(전환 후보 아님)")
                if (r.get("ext") or {}).get("runup63", 0) >= config.RECENT_RUNUP_MAX:
                    bad.append(f"{code}: now인데 3개월 급등(고점 추격)")

    if bad:
        head = "\n  ".join(bad[:12])
        more = f"\n  ...외 {len(bad)-12}건" if len(bad) > 12 else ""
        raise RuntimeError(
            f"[selfcheck] 추천/분석 불변식 위반 {len(bad)}건 — 배포 차단:\n  {head}{more}")
