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

import math

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
    # 이력 게이트(2026-08-19 외부검토 P1) — 52주 범위·장기선이 요구하는 봉수가
    #   없으면 range_pos가 '상장 이후 전 구간'으로 조용히 축소 계산된다(실측:
    #   상장 1.5년 RHLD가 급등 이력만으로 '저점권' 매수신호). **후보에서만**
    #   제외한다 — 수집·캐시·화면 노출은 그대로다. bars 키가 없는 구형 행은
    #   집계 경로가 다르므로 오탐 제외를 피하기 위해 판정하지 않는다.
    bars = r.get("bars")
    if (isinstance(bars, int) and not isinstance(bars, bool)
            and bars < config.NEWHIGH_LOOKBACK):
        reasons.append(f"이력 부족({bars}봉 — 52주 범위 산정 불가)")
    return reasons


def _stop_pct(r: dict) -> float:
    entry = r.get("entry") or 0
    stop = (r.get("risk") or {}).get("stop") or 0
    return (entry - stop) / entry if entry else 0.0


def finite_number(value, *, positive: bool = False) -> float | None:
    """판정에 쓸 수 있는 유한 실수만 통과. 아니면 None.

    bool을 거부하는 것이 핵심이다 — 파이썬에서 ``True``는 ``float(True)==1.0``이라
    조용히 "가격 1.0"으로 둔갑한다. 게이트가 숫자를 기대하는 자리에 플래그가
    흘러들면 아무도 모르게 통과한다.

    screener의 섀도 태깅과 이 게이트가 **같은 함수**를 써야 태그(trend_above_200)와
    실제 판정이 갈리지 않는다. 복제하면 나중에 한쪽만 고쳐진다.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def trend_200_reason(r: dict) -> str | None:
    """슬리브 B의 200일선 게이트. 통과면 None, 걸리면 사유 문자열.

    왜 B에만 거는가(2026-08-26 실측): 진입일 200일선 아래에서 시작한 B 청산
    9건이 **전부** 손실이고 MFE 중앙값이 0.26R이었다 — 오르다 반납한 게 아니라
    애초에 오르지 않았다. 같은 달·같은 추세 위치에서 A는 13건 중 9승이므로
    시장이 아니라 B 신호의 결함이다(Fisher p=0.0047). A에는 걸지 않는다.

    판정불가(이력 200일 미만 등)는 **거절**한다 — 매수 게이트이므로 모르는 것을
    사지 않는 쪽이 안전하고, 이 저장소의 `조회 실패 ≠ 부재` 원칙과 같은 방향이다.
    사유를 '아래'와 구분해 남겨야 이력부족으로 잃는 후보 수를 따로 셀 수 있다.
    """
    price = finite_number((r.get("sr") or {}).get("price"), positive=True)
    ma200 = finite_number((r.get("ext") or {}).get("ma200"), positive=True)
    if price is None or ma200 is None:
        return "200일선 판정불가(이력부족)"
    return None if price >= ma200 else "200일선 아래(B1)"


def _shelf_pretrend_reasons(r: dict) -> list[str]:
    """추세 게이트 **이전**까지의 탈락 사유. 빈 리스트 = 거기까진 통과.

    `classify_shelf`와 `shelf_trend_rejection`이 같은 판정을 두 번 쓰기 때문에
    한곳에 둔다. 복제하면 "필터가 걸러낸 것"과 "원래도 탈락이던 것"의 경계가
    조용히 어긋나 섀도 표본이 오염된다.
    """
    if not getattr(config, "SHELF_ENABLED", False):
        return ["shelf 비활성"]
    reasons = []
    if plan.junk(r):
        reasons.append("동전주·심한 부실")
    ccy = r.get("ccy", "USD")
    turn = r.get("turnover", 0) or 0
    liq_min = config.LIQ_MIN_KRW if ccy == "KRW" else config.LIQ_MIN_USD
    if turn < liq_min:
        reasons.append("저유동성(잡주)")
    ext = r.get("ext") or {}
    rs_rel = (r.get("rs") or {}).get("rel") or 0
    if rs_rel >= config.BLOWOFF_RATIO or ext.get("ma120_stretch", 0) >= config.BLOWOFF_RATIO:
        reasons.append("이미 폭등")           # 꼭대기 매물대 매수 방지(B에도 적용)
    if reasons:
        return reasons
    sh = r.get("shelf") or {}
    if not sh.get("ok"):
        return [sh.get("reason", "매물대 미충족")]
    return []


def shelf_trend_rejection(r: dict) -> str | None:
    """"200일선 필터가 **없었다면** B였을" 후보의 거절 사유. 아니면 None.

    B0 섀도의 모집단을 정의한다. 다른 사유로 이미 탈락한 후보는 필터와 무관하니
    None을 준다 — 섞으면 섀도가 필터의 효과가 아니라 잡동사니를 재게 된다.

    `SHELF_REQUIRE_TREND_200`과 **무관하게** 판정한다. 관측은 스위치를 꺼도
    계속돼야 필터를 껐다 켰다 하며 비교할 수 있다.
    """
    if _shelf_pretrend_reasons(r):
        return None
    return trend_200_reason(r)


def classify_shelf(r: dict) -> dict:
    """매물대 반등 슬리브(B) 판정 — {'group': 'shelf'|None, 'reasons': [...]}.

    A와 별개 신호 스트림. **하락추세 veto는 적용하지 않는다**(B는 전환 미확정·
    하락 중 지지 반등을 노림). 대신 잡주·저유동·이미폭등 하드 제외는 유지하고,
    2026-08-26 실측으로 확정된 200일선 필터(B1)를 마지막에 건다."""
    reasons = _shelf_pretrend_reasons(r)
    if reasons:
        return {"group": None, "reasons": reasons}
    # 추세 게이트는 하드 제외 **뒤**에 온다. 순서가 바뀌면 잡주가 "200일선
    #   아래"로 보고돼 제외 사유 통계가 오염된다.
    if getattr(config, "SHELF_REQUIRE_TREND_200", True):
        trend = trend_200_reason(r)
        if trend:
            return {"group": None, "reasons": [trend]}
    return {"group": "shelf", "reasons": []}


def classify_shelf_watch(r: dict) -> dict:
    """B 매물대 구조는 유효하지만 반등 확인만 덜 된 공개 관찰 후보.

    자동매수는 정확히 ``group == "shelf"``만 받는다. 이 그룹은 화면 설명용이며
    진입·수량·주문 판단에 사용하지 않는다.
    """
    if not getattr(config, "SHELF_ENABLED", False):
        return {"group": None, "reasons": ["shelf 비활성"]}
    reasons = []
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
    if reasons:
        return {"group": None, "reasons": reasons}
    shelf = r.get("shelf") or {}
    if shelf.get("ok") or not shelf.get("watch"):
        return {"group": None, "reasons": [shelf.get("reason", "관찰 대상 아님")]}
    return {"group": "shelf_watch",
            "reasons": [shelf.get("reason", "반등 확인 대기")]}


def consensus_bear(r: dict) -> float:
    """방향성 8개 지표 중 '음(-)=팔자' 비율(0~1). 보조지표 다수결 필터용.

    module_scores(각 -2..+2)에서 <0 개수 / 전체. 0점(중립)은 팔자로 세지 않는다.
    분모는 실제로 점수가 산출된 모듈 수(누락 대비). 데이터 없으면 0.0."""
    ms = r.get("module_scores") or {}
    vals = [v for v in ms.values() if isinstance(v, (int, float))]
    if not vals:
        return 0.0
    bear = sum(1 for v in vals if v < 0)
    return bear / len(vals)


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

    # 저점권(사용자 핵심): 전환 신호가 떠도 52주 범위 상단이면 '고점 추천' → 제외.
    rp = r.get("range_pos", 0.5)

    if th["now"] and stage >= 3:
        if rp > config.LOW_ZONE_NOW:
            return {"group": None, "reasons": [f"저점권 아님(범위 {rp*100:.0f}%)"]}
        if stop_pct >= config.MAX_STOP_NOW:
            return {"group": None, "reasons": ["손절폭 과대(지금진입)"]}
        if already_ran:
            return {"group": None, "reasons": ["최근 3개월 급등(고점 추격)"]}
        # 보조지표 다수결(사용자 요청) — 8개 지표 무게가 강하게 팔자면 거부.
        #   ACTIVE=False면 기록만(bear_share는 screener가 신호에 남김).
        bear = consensus_bear(r)
        if config.CONSENSUS_VETO_ACTIVE and bear >= config.CONSENSUS_BEAR_VETO:
            return {"group": None,
                    "reasons": [f"보조지표 다수 팔자({bear*100:.0f}%)"]}
        return {"group": "now", "reasons": []}

    if stage in (1, 2) or (stage >= 3 and kind in ("pullback", "breakout")):
        if rp > config.LOW_ZONE_WATCH:
            return {"group": None, "reasons": [f"저점권 아님(범위 {rp*100:.0f}%)"]}
        if stop_pct >= config.MAX_STOP_WATCH:
            return {"group": None, "reasons": ["손절폭 과대(관찰)"]}
        if far_pull:
            return {"group": None, "reasons": ["눌림 목표 과도(대폭락 대기)"]}
        return {"group": "watch", "reasons": []}

    return {"group": None, "reasons": ["전환 후보 아님(정배열-only 등)"]}


def a_gate_failures(r: dict) -> list[str] | None:
    """A('now') 게이트별 **독립** 판정 — 실패한 게이트 키 목록. 전제 미충족은 None.

    classify()는 첫 실패에서 조기 반환하므로 사유 개수로 '단일 게이트 탈락'을
    판정할 수 없다(실측: rp·runup 둘 다 걸린 후보도 사유 1개). ablation 기록
    (2026-08-19 외부검토 — 게이트 기여도 측정)은 이 함수를 쓴다. 판정·주문
    경로는 classify 그대로다 — 이 함수는 관측 전용.
    """
    if exclusion_reasons(r):
        return None
    th = plan.thesis(r)
    if not (th.get("now") and r.get("transition_stage", 0) >= 3):
        return None
    fails = []
    if r.get("range_pos", 0.5) > config.LOW_ZONE_NOW:
        fails.append("rp")
    if (r.get("ext") or {}).get("runup63", 0) >= config.RECENT_RUNUP_MAX:
        fails.append("runup")
    if (config.CONSENSUS_VETO_ACTIVE
            and consensus_bear(r) >= config.CONSENSUS_BEAR_VETO):
        fails.append("consensus")
    if _stop_pct(r) >= config.MAX_STOP_NOW:
        fails.append("stop")
    return fails


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
                if r.get("range_pos", 0.5) > config.LOW_ZONE_NOW + tol:
                    bad.append(f"{code}: now인데 저점권 아님"
                               f"(범위 {r.get('range_pos', 0.5)*100:.0f}%)")

    if bad:
        head = "\n  ".join(bad[:12])
        more = f"\n  ...외 {len(bad)-12}건" if len(bad) > 12 else ""
        raise RuntimeError(
            f"[selfcheck] 추천/분석 불변식 위반 {len(bad)}건 — 배포 차단:\n  {head}{more}")
