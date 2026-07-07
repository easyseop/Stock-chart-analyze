"""클로드 자동 모의투자 — 확정 전략(bot/STRATEGY.md)대로 스스로 진입·관리·청산.

사용자 요청: "타점 오면 알아서 들어가는 시뮬레이션, 시드 1억".
매 빌드(장중 15분)마다 이번 빌드의 현재가만 사용(예외: USD/KRW 환율만
하루 1회 갱신 — 실패해도 직전값 폴백, 빌드는 절대 안 죽음):

  ① 청산 관리 — 손절 터치(전량) → +1R(절반 익절+손절 본전) → 목표 2R(잔량) →
     타임 스탑(21일≈15거래일 내 +1R 미도달 시 정리)
  ② 지정가 체결 — 반반/눌림 전술의 대기 주문이 눌림가에 닿으면 체결,
     손절가까지 빠지면 취소(추세 훼손), 21일 지나면 만료
  ③ 신규 진입 — '지금 진입' 추천을 전술대로: ⚡즉시=전량 / ⚖반반=절반+절반 지정가 /
     ⏳눌림=지정가만. 수량은 계좌 1% 리스크, 종목당 총자산의 1/3 상한(사용자 확정)

상태: data_cache/autopaper.json (actions/cache 영속) · 공개: public/api/paper_auto.json
track.py(추천 채점)와 다른 점: 여기는 '계좌'를 굴린다 — 현금·비중·복리까지 시뮬레이션.
"""
from __future__ import annotations

import datetime
import json
import os

import config

STATE_PATH = os.path.join("data_cache", "autopaper.json")
VERSION = 4                # 규칙 바뀌면 +1 → 재시작 (v4: 장중에만 매매 — 주말 진입 무효화)
START = 100_000_000        # 시드 1억(사용자 지정)
FX = 1380                  # 달러 환산 기본값 — 하루 1회 USD/KRW 종가로 갱신(실패 시 유지)
RISK_PCT = 0.01            # 트레이드당 계좌 1% 리스크
MAX_POS = 12               # 동시 보유(대기 주문 포함) 상한
DAY_ENTRY_MAX = 3          # 하루 신규 진입 상한 — 같은 날 신호 몰림(시장 반등 베팅) 차단
DAILY_LOSS_LIMIT = 0.02    # 일일 실현손실 −2% 도달 시 당일 신규 진입 중지(서킷브레이커)
STOP_HARD_ATR = 0.5        # 하드 손절 = 손절가 − 0.5×ATR₀ 이탈 시 즉시(급락 방어)
                           #   그 위~손절가 사이는 2연속 폴링(≈30분) 확인 후 집행(소프트)
                           #   — 15분 스파이크 휩쏘 방지. 0.5는 검증 전 가설(로그로 비교)
POS_CAP = 1.0 / 3          # ★ 종목당 최대 비중 = 총자산의 1/3 (사용자 확정 기준).
                           #   한 종목에 시드의 1/3 초과 투입 금지. 사이징 단계에서
                           #   강제하고, 빌드마다 _audit_caps로 사후 재검증(위반 로그).
MAX_INVEST = 0.85          # 총 투자 한도 — 15%는 현금 버퍼(눌림 체결·신규 신호용)
TIME_STOP_DAYS = 21        # 캘린더 21일 ≈ 15거래일(config.TIME_STOP_DAYS)
PENDING_DAYS = 21          # 지정가 대기 만료
TRAIL_ATR = 3.0            # +1R 절반 익절 후 잔량 트레일 = max(본전, 최고가−3×ATR).
                           #   2R 고정 목표 대신 승자를 태움 — 110종목 174이벤트 A/B/C
                           #   백테스트에서 C(트레일 무제한 +0.131R) > A(2R 캡 +0.120R)
                           #   > B(트레일+2R캡 +0.117R), 시간 앞/뒤·한미 전 구간 일관.


def _today() -> str:
    return config.today_kst().isoformat()      # 날짜 스탬프는 KST(한국 사용자 기준)


def _market_open(ccy: str) -> bool:
    """해당 종목의 시장이 지금 장중인가 — 장 닫힌 가격(주말·야간 종가)으로
    진입/청산하는 비현실적 시뮬레이션 방지(사용자 지적: 주말 진입은 신뢰성↓).

    한국주: 평일 09:00~15:30 KST(=00:00~06:30 UTC, 요일도 이 시간대엔 UTC=KST)
    미국주: 미 동부시간(ET) 평일 09:30~16:00 — 서머타임을 zoneinfo로 정확히 반영.
      (고정 UTC 창을 쓰면 여름엔 마감 후 1시간, 겨울엔 프리마켓 1시간을
       장중으로 오판해 멈춘 종가로 가짜 체결이 남 — 사용자 지적으로 수정)
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    if ccy == "KRW":
        if now.weekday() >= 5:
            return False
        hm = now.hour * 60 + now.minute
        return 0 <= hm <= 6 * 60 + 30
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
        if et.weekday() >= 5:
            return False
        hm = et.hour * 60 + et.minute
        return 9 * 60 + 30 <= hm < 16 * 60
    except Exception:                     # tzdata 없으면 기존 고정창 폴백
        if now.weekday() >= 5:
            return False
        hm = now.hour * 60 + now.minute
        return 13 * 60 + 30 <= hm <= 21 * 60


def _age(day: str) -> int:
    try:
        return (config.today_kst() - datetime.date.fromisoformat(day)).days
    except Exception:
        return 0


def _prev_tday() -> str:
    """직전 거래일(주말 건너뜀) — 손절 쿨다운 '익일' 판정용."""
    d = config.today_kst() - datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.isoformat()


def _earnings_d(code: str):
    """어닝까지 D-일(캐시만, 실패 시 None) — 보유 중 어닝 통과 관리용."""
    try:
        from scanner import earnings
        return earnings.days_until(code)
    except Exception:
        return None


def _update_fx(st: dict) -> None:
    """USD/KRW 환산율 — 하루 1회 종가로 갱신(고정 1380의 사이징 왜곡 제거).
    실패해도 빌드는 절대 안 죽인다: 직전 성공값 → 기본값 순 폴백."""
    global FX
    if st.get("fx"):
        FX = st["fx"]
    if st.get("fx_d") == _today():
        return
    try:
        import FinanceDataReader as fdr
        start = (config.today_kst() - datetime.timedelta(days=10)).isoformat()
        v = float(fdr.DataReader("USD/KRW", start)["Close"].iloc[-1])
        if 800 < v < 2500:                 # 명백한 이상값 방어
            FX = v
            st["fx"], st["fx_d"] = v, _today()
    except Exception:
        pass


def _cooldown(st: dict, code: str) -> bool:
    """재진입 쿨다운 — 당일 전량 청산 종목은 당일 금지, 손절(-R) 종결은 +익일까지.
    15분 폴링 구조에서 손절→즉시 재매수 연쇄(churn)를 차단한다."""
    today, prev = _today(), _prev_tday()
    for c in st.get("closed", [])[:80]:
        if c["code"] != code:
            continue
        if c["closed"] == today:
            return True
        stopped = any("손절" in (e.get("note") or "") for e in c.get("exits", []))
        if stopped and c.get("r", 0) < 0 and c["closed"] == prev:
            return True
        break                              # 이 종목의 최신 청산만 보면 됨
    return False


def _day_ent(st: dict) -> dict:
    """오늘 신규 진입 카운터(날짜 바뀌면 리셋)."""
    de = st.get("day_ent")
    if not de or de.get("d") != _today():
        de = {"d": _today(), "n": 0}
        st["day_ent"] = de
    return de


def _today_pl(st: dict) -> float:
    """오늘 실현손익(원) — 일일 서킷브레이커 판정용."""
    return sum(e.get("pl", 0) or 0 for e in st["log"]
               if e.get("type") == "sell" and e.get("d") == _today())


def _krw(v: float, ccy: str) -> float:
    return v * FX if ccy == "USD" else v


def _valid(st) -> bool:
    return (isinstance(st, dict) and st.get("start") == START
            and "pos" in st and st.get("v") == VERSION)


def _read(path: str):
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return None


def _state_branch_snapshot():
    """state 브랜치의 계좌 스냅샷 조회(git) — cache 소멸 시 3차 복구 경로.
    git 부재·브랜치 부재·파싱 실패 전부 조용히 None(빌드는 절대 안 죽임).

    ※ CI(GitHub Actions)에서만 작동 — 로컬 테스트/개발이 빈 상태로 시작할 때
      운영 계좌를 몰래 끌어오면 테스트 격리가 깨지고(실측: state 브랜치 생성
      직후 전체 테스트가 운영 잔고로 오염) 개발 실수로 운영 상태를 만질 위험."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return None
    try:
        import subprocess
        subprocess.run(["git", "fetch", "-q", "origin", "state"],
                       timeout=30, check=True, capture_output=True)
        out = subprocess.run(
            ["git", "show", "FETCH_HEAD:state/autopaper.snapshot.json"],
            timeout=10, check=True, capture_output=True)
        return json.loads(out.stdout)
    except Exception:
        return None


def _load() -> dict:
    """상태 로드 — 복구 순서 고정(SRE 검토 §2 채택):
      ① 본파일 → ② .bak → ③ state 브랜치 스냅샷(git 영구 백업) → ④ 새 계좌.

    자동매매에서 '계좌가 이유 없이 리셋'되는 사고를 막는다: 원자적 저장으로
    본파일 손상을 예방하고, cache가 통째로 소멸(7일 미사용·임의 evict)해도
    state 브랜치 백업으로 되살린다.
    """
    st = _read(STATE_PATH)
    if _valid(st):
        return st
    bak = _read(STATE_PATH + ".bak")
    if _valid(bak):
        print("[autopaper] 본 상태파일 손상 → 백업(.bak)에서 복구")
        return bak
    snap = _state_branch_snapshot()
    if _valid(snap):
        print("[autopaper] 로컬 상태 없음 → state 브랜치 스냅샷에서 복구")
        return snap
    if st is not None or bak is not None:
        # 파일은 있는데 유효하지 않음(버전 변경 등) — 초기화는 의도된 경우만
        print("[autopaper] 유효한 상태 없음 → 새 계좌로 시작(시드 1억)")
    return {"v": VERSION, "cash": START, "start": START,
            "pos": {}, "pending": {}, "log": []}


def _save(st: dict) -> None:
    """원자적 저장 — 임시파일에 쓰고 os.replace로 교체(중간에 죽어도 손상 없음).
    교체 직전의 정상본을 .bak으로 남겨 복구 경로 확보."""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(st, fp, ensure_ascii=False)
        fp.flush()
        os.fsync(fp.fileno())
    if os.path.exists(STATE_PATH):        # 직전 정상본을 백업으로 보존
        try:
            os.replace(STATE_PATH, STATE_PATH + ".bak")
        except OSError:
            pass
    os.replace(tmp, STATE_PATH)           # 원자적 교체


def _log(st: dict, typ: str, code: str, name: str, q: int, price: float,
         ccy: str, note: str, pl: float | None = None,
         pl_pct: float | None = None, r: float | None = None,
         stop: float | None = None, target: float | None = None) -> None:
    ent = {"d": _today(), "type": typ, "code": code, "name": name,
           "q": q, "price": round(price, 4), "ccy": ccy,
           "amt": round(_krw(price, ccy) * q), "note": note}
    if pl is not None:
        ent["pl"] = round(pl)
        ent["pl_pct"] = round(pl_pct or 0, 1)
    if r is not None:
        ent["r"] = r                       # 청산 완료 트레이드의 R 성과
    if stop is not None:
        ent["stop"] = round(stop, 4)       # 매수 시 잡은 손절가(상세 보고용)
    if target is not None:
        ent["target"] = round(target, 4)
    st["log"].insert(0, ent)
    st["log"] = st["log"][:200]
    st.setdefault("_ev", []).append(ent)   # 이번 런 이벤트 — 텔레그램 보고용


def _sell(st: dict, code: str, q: int, price: float, note: str) -> None:
    p = st["pos"][code]
    q = min(q, p["q"])
    if q <= 0:
        return
    st["cash"] += _krw(price, p["ccy"]) * q
    pl = (_krw(price, p["ccy"]) - _krw(p["avg"], p["ccy"])) * q   # 실현 손익(원)
    pct = (price / p["avg"] - 1) * 100 if p["avg"] else 0
    p["realized"] = p.get("realized", 0) + pl
    p.setdefault("exits", []).append({"d": _today(), "q": q,
                                      "price": round(price, 4), "note": note})
    p["q"] -= q
    final = p["q"] <= 0
    r = None
    if final and p.get("risk0"):
        r = round(p["realized"] / p["risk0"], 2)     # 트레이드 성과(R 단위)
    _log(st, "sell", code, p["name"], q, price, p["ccy"], note, pl, pct, r=r)
    if final:
        # 청산 완료 — 예측(계획) vs 실제를 통째로 기록(승률·기준 준수 분석용)
        st.setdefault("closed", []).insert(0, {
            "code": code, "name": p["name"], "ccy": p["ccy"],
            "ctx": p.get("ctx"),             # 매수 사유(신선도·단계·저점권·체크·전술)
            "plan": p.get("plan"),           # 진입 시점 우리 기준(진입/손절/목표)
            "fill": {"avg": round(p["avg"], 4), "q": p.get("q0", 0)},  # 실제 체결
            "exits": p["exits"],             # 실제 청산 내역(가격·사유)
            "opened": p["opened"], "closed": _today(),
            "pl": round(p["realized"]), "r": r if r is not None else 0,
        })
        st["closed"] = st["closed"][:400]
        del st["pos"][code]


def _buy(st: dict, code: str, name: str, ccy: str, q: int, price: float,
         stop: float, target: float, note: str,
         plan: dict | None = None, ctx: dict | None = None,
         atr0: float | None = None) -> bool:
    cost = _krw(price, ccy) * q
    if q < 1 or cost > st["cash"]:
        q = int(st["cash"] // _krw(price, ccy))
        cost = _krw(price, ccy) * q
        if q < 1:
            return False
    st["cash"] -= cost
    p = st["pos"].get(code)
    if p:                                  # 반반 2차 체결 — 평단 합산 + 리스크 누적
        p["avg"] = (p["avg"] * p["q"] + price * q) / (p["q"] + q)
        p["q"] += q
        p["q0"] = p.get("q0", 0) + q
        p["risk0"] = p.get("risk0", 0) + _krw(price - stop, ccy) * q
    else:
        st["pos"][code] = {"name": name, "ccy": ccy, "q": q, "q0": q,
                           "avg": price, "entry": price, "stop": stop,
                           "stop0": stop,               # 진입 시점 손절(MFE·R 기준선)
                           "target": target, "half_done": False,
                           "opened": _today(),
                           "plan": plan, "ctx": ctx,
                           "atr0": atr0, "hw": price,   # 트레일용: 진입 ATR·최고가
                           "risk0": _krw(price - stop, ccy) * q,
                           "realized": 0, "exits": []}
    _log(st, "buy", code, name, q, price, ccy, note, stop=stop, target=target)
    return True


def _basis(item: dict) -> str:
    """매수 사유 한 줄 — '왜 샀는지'를 기록(전부 추천 기준에서 나온 값들)."""
    t = item.get("tactic") or {}
    parts = [item.get("freshness"), item.get("stage"),
             f'저점권{item.get("range_pos", "?")}%',
             f'체크{item.get("rec", "?")}/6',
             f'손절폭{t.get("stop_pct", "?")}%']
    return " · ".join(str(x) for x in parts if x)


def _ctx(item: dict) -> dict:
    t = item.get("tactic") or {}
    return {"mode": t.get("mode", "full"), "fresh": bool(item.get("fresh")),
            "stage": item.get("stage_n", 0), "rp": item.get("range_pos"),
            "rec": item.get("rec"), "basis": _basis(item)}


def _stats(st: dict) -> dict:
    """청산 완료 트레이드로 승률·평균R을 기준별(전술/신선도)로 집계 +
    예측(계획) 대비 실제 체결 편차."""
    cl = st.get("closed", [])

    def agg(rows):
        n = len(rows)
        w = sum(1 for x in rows if x["r"] > 0)
        return {"n": n, "win": w,
                "win_rate": round(w / n * 100) if n else 0,
                "avg_r": round(sum(x["r"] for x in rows) / n, 2) if n else 0}

    by_mode = {}
    for m in ("full", "half", "pullback"):
        rows = [x for x in cl if (x.get("ctx") or {}).get("mode") == m]
        if rows:
            by_mode[m] = agg(rows)
    fresh = agg([x for x in cl if (x.get("ctx") or {}).get("fresh")])
    stale = agg([x for x in cl if x.get("ctx") and not x["ctx"].get("fresh")])
    # 전환 단계별(③ vs ④) — 리스크 티어링 결정용 포워드 데이터(2차 검토 Phase 1)
    by_stage = {}
    for sn in (3, 4):
        rows = [x for x in cl if (x.get("ctx") or {}).get("stage") == sn]
        if rows:
            by_stage[str(sn)] = agg(rows)
    # 시장별(KR/US) — KR 음(-) 기대값 divergence 추적용(2차 검토 추가-5)
    by_ccy = {}
    for ccy in ("KRW", "USD"):
        rows = [x for x in cl if x.get("ccy") == ccy]
        if rows:
            by_ccy[ccy] = agg(rows)
    # 예측 vs 실제 — 계획 진입가 대비 실제 평단(+ = 비싸게 삼), 계획 손절가 대비 손절 체결가
    slips = [(x["fill"]["avg"] / x["plan"]["entry"] - 1) * 100
             for x in cl if x.get("plan") and x["plan"].get("entry")
             and x.get("fill", {}).get("avg")]
    stop_slips = []
    for x in cl:
        ps = (x.get("plan") or {}).get("stop")
        if not ps:
            continue
        for e in x.get("exits", []):
            if "손절" in e["note"]:
                stop_slips.append((e["price"] / ps - 1) * 100)
    return {"all": agg(cl), "by_mode": by_mode, "fresh": fresh, "stale": stale,
            "by_stage": by_stage, "by_ccy": by_ccy,
            "slip_entry_pct": round(sum(slips) / len(slips), 2) if slips else None,
            "slip_stop_pct": (round(sum(stop_slips) / len(stop_slips), 2)
                              if stop_slips else None)}


def _equity(st: dict, px: dict) -> float:
    total = st["cash"]
    for code, p in st["pos"].items():
        cur = px.get(code, (None, p["avg"], p["ccy"]))[1]
        total += _krw(cur, p["ccy"]) * p["q"]
    return total


def _fmt_native(v: float, ccy: str) -> str:
    return f"{v:,.0f}원" if ccy == "KRW" else f"${v:,.2f}"


def _audit_caps(st: dict, equity: float) -> list[dict]:
    """사후 재검증 — 어떤 종목도 '투입 원가(보유+대기 합산)'가 총자산의 1/3을
    넘지 않는지 빌드마다 재확인. 사이징에서 이미 막지만, 반반 전술이 pos+pending에
    동시에 걸리는 등 경로가 많아 '실제로 안 넘는지'를 독립적으로 감사한다.

    기준은 '투입 원가'(보유=평단×수량, 대기=지정가×수량). 진입 후 주가가 올라
    평가액이 1/3을 넘는 것은 정상(승자를 강제로 자르지 않음) — 감사 대상 아님.
    위반이 있으면 로그로 남기고(조용한 룰 위반 방지) 목록을 반환한다.
    """
    cap = equity * POS_CAP
    codes = set(st["pos"]) | set(st["pending"])
    violations = []
    for code in codes:
        cost = 0.0
        p = st["pos"].get(code)
        if p:
            cost += _krw(p["avg"], p["ccy"]) * p["q"]        # 보유 원가
        o = st["pending"].get(code)
        if o:
            cost += _krw(o["limit"], o["ccy"]) * o["q"]      # 대기 주문 원가
        # 1원 단위 반올림·환율 오차 흡수용 0.5% 여유
        if cost > cap * 1.005:
            name = (p or o).get("name", code)
            violations.append({"code": code, "name": name,
                               "cost": round(cost), "cap": round(cap),
                               "pct": round(cost / equity * 100, 1)})
    if violations:
        for v in violations:
            print(f"[autopaper] ⚠️ 비중 위반 {v['name']}({v['code']}): "
                  f"투입 {v['cost']:,}원 = {v['pct']}% > 상한 {v['cap']:,}원(1/3)")
    return violations


def _audit_rules(st: dict) -> list[str]:
    """운영 규칙 사후 감사 — 위반은 로그·공개 JSON에 남긴다(조용한 위반 방지).
    ① 하루 신규 진입 ≤ DAY_ENTRY_MAX ② 당일 손절 종목이 pos/pending에 재등장 금지."""
    bad = []
    de = st.get("day_ent") or {}
    if de.get("d") == _today() and de.get("n", 0) > DAY_ENTRY_MAX:
        bad.append(f"하루 신규 진입 {de['n']}건 > 상한 {DAY_ENTRY_MAX}")
    stopped_today = {e["code"] for e in st["log"]
                     if e.get("type") == "sell" and e.get("d") == _today()
                     and "손절" in (e.get("note") or "")}
    for code in stopped_today:
        if code in st["pos"] or code in st["pending"]:
            bad.append(f"{code}: 당일 손절 후 pos/pending 재등장(쿨다운 위반)")
    for b in bad:
        print(f"[autopaper] ⚠️ 규칙 위반: {b}")
    return bad


def _report_violations(st: dict, cap_viol: list, rule_viol: list) -> None:
    """규칙/비중 위반을 텔레그램으로 즉시 에스컬레이션 — 자동매매가 '조용히
    규칙을 어기는' 것을 사람이 대시보드를 안 봐도 알게 한다.

    같은 위반은 하루 한 번만 알림(15분마다 재알림 방지 — 위반 집합이 바뀌면 재알림).
    감사(_audit_*)는 이미 매 빌드 도는데, 그 결과가 JSON에만 쌓이고 아무도 안 보던
    구멍을 메운다(사용자 지적: '발생 전에 대비'해야 한다).
    """
    items = ([f"비중 {v['name']}({v['code']}) {v['pct']}%>33%" for v in cap_viol]
             + list(rule_viol))
    if not items:
        return
    sig = _today() + "|" + "|".join(sorted(items))
    if st.get("_viol_sig") == sig:
        return                             # 같은 위반 이미 알림함(당일)
    st["_viol_sig"] = sig
    text = ("🚨 <b>자동매매 규칙 위반 감지</b> (즉시 점검)\n"
            + "\n".join("· " + x for x in items)
            + "\n→ 사이징·쿨다운·상한 로직 확인 필요.")
    try:
        from bot import notify
        notify.send(text)
    except Exception:
        pass


def _report(st: dict, equity: float) -> None:
    """이번 런의 매수/매도/주문을 텔레그램으로 보고(체결 있을 때만).

    토큰 미설정/전송 실패는 조용히 무시 — 시뮬레이션(빌드)은 절대 안 죽인다.
    """
    ev = st.pop("_ev", [])
    if not any(x["type"] in ("buy", "sell") for x in ev):
        return

    def _blk(x) -> str:
        """이벤트 1건을 상세 블록으로 — 진입가·손절·목표·수량·사유·손익까지."""
        f = lambda v: _fmt_native(v, x["ccy"])
        # note는 "즉시 분할 — 🔥 갓 전환 · ④ 확정 · 저점권27% · 체크6/6 · 손절폭5.0%"
        head, _, why = x["note"].partition(" — ")
        if x["type"] == "buy":
            ln = (f'🟢 <b>매수 · {x["name"]}</b>\n'
                  f'  {x["q"]}주 @ {f(x["price"])} = {x["amt"]:,}원\n'
                  f'  손절 {f(x["stop"])} · 목표 {f(x["target"])}\n'
                  f'  방식 {head}')
            if why:
                ln += f'\n  사유 {why}'
            return ln
        if x["type"] == "sell":
            sg = "+" if x.get("pl", 0) >= 0 else ""
            pl = (f' → <b>{sg}{x["pl"]:,}원 ({sg}{x["pl_pct"]}%'
                  + (f' · {"+" if x["r"]>=0 else ""}{x["r"]}R' if x.get("r") is not None else "")
                  + ')</b>') if x.get("pl") is not None else ""
            return (f'🔴 <b>매도 · {x["name"]}</b> [{head}]\n'
                    f'  {x["q"]}주 @ {f(x["price"])}{pl}')
        if x["type"] == "order":
            return f'📌 <b>지정가 주문 · {x["name"]}</b> {x["q"]}주 @ {f(x["price"])} ({head})'
        return f'✖ <b>주문 취소 · {x["name"]}</b> ({head})'

    blocks = [_blk(x) for x in ev[:10]]
    if len(ev) > 10:
        blocks.append(f'…외 {len(ev) - 10}건')
    ret = (equity / st["start"] - 1) * 100
    sg = "+" if ret >= 0 else ""
    text = ("🤖 <b>자동매매 체결 상세</b> (시드 1억 · 가상)\n\n"
            + "\n\n".join(blocks)
            + f"\n\n───────\n💰 평가 <b>{equity:,.0f}원</b> "
              f"({sg}{ret:.2f}%) · 현금 {st['cash']:,.0f}원 · 보유 {len(st['pos'])}종목")
    try:
        from bot import notify
        notify.send(text)
    except Exception:
        pass


def update(results: list[dict], picks: dict, out_dir: str = "public") -> dict:
    """빌드마다 호출 — 전략대로 시뮬레이션 한 스텝 진행하고 요약 반환."""
    st = _load()
    st["_ev"] = []
    _update_fx(st)                         # 환산율 갱신(하루 1회, 실패 시 직전값)
    px = {}
    for r in results:
        p = (r.get("sr") or {}).get("price")
        if p:
            px[r["code"]] = (r["name"], float(p), r.get("ccy", "USD"))

    # ① 보유 관리 — 하드/소프트 손절 → +1R 절반(2연속 확인) → 잔량 트레일 래칫
    #    → 어닝 D-1 관리 → 타임 스탑. 장중인 시장의 종목만.
    #    '터치 즉시' 집행의 15분 스파이크 휩쏘를 줄이기 위해 손절·+1R 모두
    #    2연속 폴링(≈30분) 확인 후 집행하고, 급락은 하드선(−0.5ATR)이 즉시 방어.
    for code in list(st["pos"].keys()):
        p = st["pos"][code]
        cur = px.get(code)
        if not cur or not _market_open(p["ccy"]):
            continue
        price = cur[1]
        p["hw"] = max(p.get("hw", price), price)       # MFE 추적(어닝 관리·트레일)
        stop0 = p.get("stop0") or (p.get("plan") or {}).get("stop") or p["stop"]
        risk_ps = p["entry"] - stop0                   # 1주당 초기 리스크(R 기준선)
        one_r = p["entry"] + risk_ps
        # 하드선: 손절가 − 0.5×ATR₀(급락은 확인 없이 즉시). ATR 미기록(구)은 터치 즉시.
        hard = p["stop"] - STOP_HARD_ATR * p["atr0"] if p.get("atr0") else p["stop"]

        def _stop_note() -> str:
            if not p["half_done"]:
                return "손절"
            return "트레일 스탑" if p["stop"] > p["entry"] else "본전 스탑"

        if price <= hard:
            _sell(st, code, p["q"], price, _stop_note() + "(급락 즉시)"
                  if p.get("atr0") else _stop_note())
        elif price <= p["stop"]:
            if p.get("stop_hit"):                      # 2연속 확인 — 소프트 집행
                _sell(st, code, p["q"], price, _stop_note())
            else:
                p["stop_hit"] = True
        else:
            p["stop_hit"] = False
            if not p["half_done"]:
                if price >= one_r:
                    if p.get("r1_hit"):                # +1R도 2연속 확인(스파이크 방지)
                        _sell(st, code, max(1, p["q"] // 2), price, "+1R 절반 익절")
                        if code in st["pos"]:          # 잔량 → 손절 본전(래칫)
                            p["stop"] = max(p["stop"], p["entry"])
                            p["half_done"] = True
                    else:
                        p["r1_hit"] = True
                else:
                    p["r1_hit"] = False
            if code not in st["pos"]:
                continue                               # 전량 청산됨(q=1 절반익절 등)
            if p["half_done"] and p.get("atr0"):
                # 잔량 = ATR 트레일 래칫(2R 캡 없음). 스탑 판정은 위에서 '이전
                # 트레일'로 먼저 했으므로 여기서 갱신(선반영 방지 — 백테스트 동일).
                p["stop"] = max(p["stop"], p["hw"] - TRAIL_ATR * p["atr0"])
            elif p["half_done"] and price >= p["target"]:
                # 구(舊) 포지션(진입 ATR 미기록) — 기존 2R 목표 규칙 유지
                _sell(st, code, p["q"], price, "목표(2R) 도달")
                continue
            # 어닝 D-1 — 갭은 손절선을 무시하므로 쿠션 없는 포지션은 정리.
            #   MFE<0.5R 전량 / 0.5~1R 절반 / +1R 달성(본전 잠김)은 통과 허용.
            ed = None if p.get("earn_ok") else _earnings_d(code)
            if ed is not None and 0 <= ed <= 1:
                if p["half_done"]:
                    p["earn_ok"] = True
                else:
                    mfe_r = (p["hw"] - p["entry"]) / risk_ps if risk_ps > 0 else 0
                    if mfe_r < 0.5:
                        _sell(st, code, p["q"], price,
                              "어닝 회피 — 전량(진행 없음, MFE<0.5R)")
                    else:
                        _sell(st, code, max(1, p["q"] // 2), price,
                              "어닝 축소 — 절반(MFE<1R)")
                        if code in st["pos"]:
                            p["earn_ok"] = True
            elif _age(p["opened"]) >= TIME_STOP_DAYS and not p["half_done"]:
                _sell(st, code, p["q"], price, "타임 스탑(+1R 미도달)")

    # ② 지정가 대기 체결/취소 (주문 금액은 ③에서 예약해둔 상태 — 체결 실패 시 취소)
    #    체결 판정 전에 '신호 부패' 재검사 — 주문 생성 후 종목이 하드 제외
    #    (부실·저유동·폭등 등)에 걸리거나 어닝 D-3에 들어오면 취소.
    #    "추천 기준으로만 매수한다" 원칙이 대기 주문 경로에서도 유지되게.
    rmap = {r["code"]: r for r in results}
    for code in list(st["pending"].keys()):
        o = st["pending"][code]
        rot = None
        r = rmap.get(code)
        if r is not None:
            try:
                from scanner import gates
                why = gates.exclusion_reasons(r)
                if why:
                    rot = "제외 게이트 — " + ",".join(why)
            except Exception:
                pass                       # 재검사 실패는 취소 사유 아님(보수적)
        if rot is None:
            ed = _earnings_d(code)
            if ed is not None and 0 <= ed <= 3:
                rot = "어닝 D-3 이내(신규 진입 금지 구간)"
        if rot:
            cur = px.get(code)
            _log(st, "cancel", code, o["name"], 0, cur[1] if cur else o["limit"],
                 o["ccy"], "지정가 취소 — 신호 부패: " + rot)
            del st["pending"][code]
    for code in list(st["pending"].keys()):
        o = st["pending"][code]
        cur = px.get(code)
        if not cur or not _market_open(o["ccy"]):
            continue
        price = cur[1]
        if price <= o["stop"]:
            _log(st, "cancel", code, o["name"], 0, price, o["ccy"],
                 "지정가 취소 — 손절선 이탈(추세 훼손)")
            del st["pending"][code]
        elif price <= o["limit"]:
            filled = _buy(st, code, o["name"], o["ccy"], o["q"], o["limit"],
                          o["stop"], o["target"],
                          "눌림 지정가 체결 — " + (o.get("basis") or ""),
                          plan=o.get("plan"), ctx=o.get("ctx"),
                          atr0=o.get("atr"))
            if not filled:
                _log(st, "cancel", code, o["name"], 0, price, o["ccy"],
                     "지정가 취소 — 현금 부족")
            # ※ 체결은 일일 진입 카운트에 넣지 않는다 — '진입 결정'은 주문을 낸
            #   날 ③에서 이미 +1 됐다. 체결일에 또 세면 이중 카운트가 되고,
            #   전일 주문이 오늘 체결되면 오늘 결정 0건에도 위반 오탐이 난다
            #   (실측 2026-07-07 12:21 경보 — 셀트리온 전일 주문 체결 건).
            del st["pending"][code]
        elif _age(o["created"]) >= PENDING_DAYS:
            _log(st, "cancel", code, o["name"], 0, price, o["ccy"],
                 "지정가 만료 — 눌림 안 옴(놓침≠손실)")
            del st["pending"][code]

    # ③ 신규 진입 — '지금 진입' 추천을 전술대로.
    #    지정가 대기 주문 금액은 '예약'으로 계산해 현금을 다 써버리지 않는다
    #    (실제 증권사의 주문가능금액과 동일 개념 — 대기 주문이 체결 불능이 되는 것 방지)
    equity = _equity(st, px)
    reserved = sum(_krw(o["limit"], o["ccy"]) * o["q"]
                   for o in st["pending"].values())
    invested = equity - st["cash"]                 # 현재 보유 평가액
    budget = min(st["cash"] - reserved,
                 equity * MAX_INVEST - invested - reserved)
    day_pl = _today_pl(st)
    halted = day_pl <= -equity * DAILY_LOSS_LIMIT  # 일일 서킷브레이커
    if halted:
        print(f"[autopaper] ⛔ 일일 실현손실 {day_pl:,.0f}원 "
              f"(한도 −{DAILY_LOSS_LIMIT*100:.0f}%) — 당일 신규 진입 중지")
    de = _day_ent(st)
    for item in picks.get("now", []):
        if halted:
            break
        code = item["code"]
        if code in st["pos"] or code in st["pending"]:
            continue
        if _cooldown(st, code):
            continue                       # 손절/청산 직후 재진입 금지(churn 방지)
        if de["n"] >= DAY_ENTRY_MAX:
            break                          # 하루 신규 진입 상한 — 몰림 차단
        if len(st["pos"]) + len(st["pending"]) >= MAX_POS:
            break
        if budget <= 0:
            break                                      # 주문가능금액 소진
        entry, stop, target = item.get("price"), item["stop"], item["target"]
        if not (entry and stop and target and stop < entry):
            continue
        ccy = item["ccy"]
        if not _market_open(ccy):
            continue                       # 장 닫힘 — 종가로 가짜 진입 금지
        ed = item.get("earnings_d")
        if ed is not None and 0 <= ed <= 3:
            continue                       # 어닝 D-3 이내 — 갭 리스크, 신규 진입 금지
        per = _krw(entry, ccy)                         # 1주 가격(원)
        risk_share = _krw(entry - stop, ccy)           # 1주당 리스크(원)
        q = int(equity * RISK_PCT // risk_share) if risk_share > 0 else 0
        cap_q = int(equity * POS_CAP // per)           # 종목당 총자산 1/3 상한(주수)
        q = min(q, cap_q)                              # 리스크·비중 중 더 빡빡한 쪽
        if q < 1:
            continue
        t = item.get("tactic") or {}
        mode, pb = t.get("mode", "full"), t.get("pb_price")
        atr0 = item.get("atr") or None     # 진입 시점 ATR — 잔량 트레일 래칫용
        # 예측값(우리 기준) 스냅샷 + 매수 사유 — 추천 기준으로만 매수한다는 증빙
        plan = {"entry": entry, "stop": stop, "target": target,
                "pb": pb, "tactic": mode}
        ctx, basis = _ctx(item), _basis(item)
        if mode == "pullback":
            if pb and stop < pb < entry:
                oq = min(q, int(budget // _krw(pb, ccy)))
                if oq < 1:
                    continue
                st["pending"][code] = {"name": item["name"], "ccy": ccy,
                                       "limit": pb, "stop": stop, "target": target,
                                       "q": oq, "created": _today(), "atr": atr0,
                                       "plan": plan, "ctx": ctx, "basis": basis}
                budget -= _krw(pb, ccy) * oq           # 주문 금액 예약
                de["n"] += 1                           # 신규 진입 결정 카운트
                _log(st, "order", code, item["name"], oq, pb, ccy,
                     "눌림 지정가 주문(추격 금지) — " + basis)
            continue                                   # 눌림가 없으면 관망
        if mode == "half":
            bq = min(max(1, q // 2), int(budget // per))
            if bq < 1:
                continue
            if _buy(st, code, item["name"], ccy, bq, entry, stop, target,
                    "반반 1차 — " + basis, plan=plan, ctx=ctx, atr0=atr0):
                budget -= per * bq
                de["n"] += 1
            oq = q - q // 2
            if pb and stop < pb < entry and oq >= 1:
                oq = min(oq, int(budget // _krw(pb, ccy)))
                if oq >= 1:
                    st["pending"][code] = {"name": item["name"], "ccy": ccy,
                                           "limit": pb, "stop": stop,
                                           "target": target, "q": oq,
                                           "created": _today(), "atr": atr0,
                                           "plan": plan, "ctx": ctx, "basis": basis}
                    budget -= _krw(pb, ccy) * oq
            continue
        bq = min(q, int(budget // per))
        if bq >= 1 and _buy(st, code, item["name"], ccy, bq, entry, stop, target,
                            "즉시 분할 — " + basis, plan=plan, ctx=ctx, atr0=atr0):
            budget -= per * bq
            de["n"] += 1

    equity = _equity(st, px)
    cap_viol = _audit_caps(st, equity)   # 종목당 1/3 상한 사후 재검증(위반 로그)
    rule_viol = _audit_rules(st)         # 일일 상한·쿨다운 사후 재검증(위반 로그)
    _report_violations(st, cap_viol, rule_viol)   # 위반 시 텔레그램 즉시 경보
    _report(st, equity)      # 이번 런 체결 내역 텔레그램 보고(_ev 소비)
    # 수익 곡선용 일별 스냅샷 — 하루 1점(같은 날 재실행 시 최신값으로 갱신)
    hist = st.setdefault("hist", [])
    today = _today()
    if hist and hist[-1]["d"] == today:
        hist[-1]["v"] = round(equity)
    else:
        hist.append({"d": today, "v": round(equity)})
    st["hist"] = hist[-400:]
    _save(st)
    positions = []
    for code, p in st["pos"].items():
        cur = px.get(code, (p["name"], p["avg"], p["ccy"]))[1]
        pl = (_krw(cur, p["ccy"]) - _krw(p["avg"], p["ccy"])) * p["q"]
        positions.append({"code": code, "name": p["name"], "ccy": p["ccy"],
                          "q": p["q"], "avg": p["avg"], "price": cur,
                          "stop": p["stop"], "target": p["target"],
                          "half_done": p["half_done"], "opened": p["opened"],
                          "pl_krw": round(pl),
                          "pl_pct": round((cur / p["avg"] - 1) * 100, 2)})
    stats = _stats(st)
    out = {
        "updated": _today(), "start": st["start"], "cash": round(st["cash"]),
        "equity": round(equity),
        "ret_pct": round((equity / st["start"] - 1) * 100, 2),
        "positions": positions,
        "pending": [{"code": c, **o} for c, o in st["pending"].items()],
        "trades": stats["all"]["n"], "win_trades": stats["all"]["win"],
        "pos_cap_pct": round(POS_CAP * 100, 1),   # 종목당 비중 상한(=33.3%)
        "cap_violations": cap_viol,        # 1/3 상한 위반(정상 시 빈 배열)
        "rule_violations": rule_viol,      # 일일상한·쿨다운 위반(정상 시 빈 배열)
        "fx": round(FX, 2),                # 이번 빌드 적용 환산율
        "stats": stats,                    # 승률·평균R(전술/신선도별) + 예측대비 편차
        "closed": st.get("closed", [])[:30],   # 청산 기록 — 계획 vs 실제 비교용
        "hist": st.get("hist", []),        # 수익 곡선용 일별 평가액 스냅샷
        "log": st["log"][:50],
    }
    os.makedirs(os.path.join(out_dir, "api"), exist_ok=True)
    with open(os.path.join(out_dir, "api", "paper_auto.json"), "w",
              encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    return out
