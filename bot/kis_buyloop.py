"""매수 루프(Loop B) — **신선한 스캐너 신호를 KIS 시세로 직접 집행**.

브레인/장부/손 아키텍처의 '손 - 매수' 쪽. 파수꾼(손절 매도)의 대칭.

정정(2026-08-05): KIS는 scanner/autopaper(가상 시뮬레이터)의 보유·진입일·가상
체결을 따라 사는 미러 계좌가 **아니다**. 공유하는 것은 전략 규칙(신호 선정·
전술·진입/손절가)뿐이고, 계좌 상태의 진실은 KIS 브로커·원장이다. autopaper의
가상 계좌상태(보유·진입일·평단·pending·하루 3건)는 KIS 주문 권한과 무관하며
이 모듈은 그것을 읽지 않는다.
  · 스캐너 'now' 신호(신선·유효 entry/stop)를 후보로, KIS 현재가가 진입 조건에
    들어올 때만 주문을 검토한다.
  · **브로커가 진실**: 매수 전 KIS 잔고(KR+미 3거래소 병합)를 재조회해
    이미 보유한 종목은 건너뛴다(중복매수 금지). 조회 실패=보수적 전면 skip
    (전역 포지션 캡을 검증할 수 없으면 이번 사이클은 안 산다 — fail-closed).
  · 가격 괴리 가드: 현재가가 신호 진입가 ±ENTRY_TOLERANCE 밖이면 skip(늦은 미러 방지).
  · 공유 전략규칙 게이트(autopaper와 같은 규칙, 2026-07-15): 어닝 D-3 이내 skip ·
    당일 매도(손절) 종목 재진입 금지(쿨다운) — 페이퍼 시뮬과 같은 규칙.
  · 롤아웃·예산 입력도 브로커-진실: 포지션 수(n_open)·투입원가(open_cost)를
    잔고에서 계산해 넘긴다. mirror는 n_open으로 동시 보유 수를 제한하지 않지만
    하위 Stage는 계속 사용한다. open_cost는 사이클 안에서도 매수마다 즉시 누적해
    같은 스냅샷의 연속 매수가 SEED를 초과하는 구멍을 차단한다.
  · 실제 전송은 kis_buy.execute_entry의 게이트 체인(ALLOW_BUY·kill·boot·SLA·
    rollout·ownership·ledger·sizing·place)을 전부 통과해야만. 이 모듈은 '무엇을
    시도할지'만.

이 모듈은 스스로 루프 돌지 않는다 — 서버 루프B(또는 검증)가 run_once를 주기 호출.
체결 확정은 kis_boot._resolve_acks(잔고대사)가 담당(매 사이클 자동).
"""
from __future__ import annotations

import datetime
import math
import os
import sys
import time

from bot import envelope, kis, kis_buy, kis_pending, kis_positions, settings

_US_EXCGS = ("NASD", "NYSE", "AMEX")   # 보유 병합용 — NYSE/AMEX 보유 누락 방지
_KST = datetime.timezone(datetime.timedelta(hours=9))


def _now_signals(signals: list[dict]) -> list[dict]:
    """'지금 진입'·신선·진입/손절 유효 신호만. 정렬: 갓전환·상위단계·상위점수."""
    # entry/stop은 **키 존재만** 확인한다 — truthy 검사면 stop=0 같은 무효값이
    #   진단 없이 후보에서 사라진다(Codex V3 P2). 숫자 유효성은 run_once의
    #   공통 input 게이트가 판정해 일관된 gate=input을 남긴다.
    cand = [s for s in signals
            if s.get("group") == "now" and s.get("fresh") is True
            and "entry" in s and "stop" in s]
    cand.sort(key=lambda s: (not s.get("fresh"), -s.get("stage", 0),
                             -s.get("norm", 0)))
    return cand


def _sold_today(fold: dict) -> set[str]:
    """오늘(KST) SELL 제출이 있는 종목들 — 당일 손절/청산 재진입 금지.
    공유 전략규칙(당일 손절 종목 재등장 금지) — 판정 근거는 KIS 원장뿐."""
    day = datetime.datetime.now(_KST).date()
    out: set[str] = set()
    for cur in fold.values():
        if (cur.get("side") or "").upper() != "SELL" or not cur.get("symbol"):
            continue
        ts = cur.get("submitted_at") or 0
        if ts and datetime.datetime.fromtimestamp(ts, _KST).date() == day:
            out.add(str(cur["symbol"]).upper())
    return out


def _broker_state(fx: float):
    """미러 게이트 입력(브로커-진실). 반환:
       (held, held_cost{code:원가KRW}, reservations[list], sold_today,
        held_sleeves{code:A|B})
    또는 조회 실패 시 None.

    · 보유는 KR + 미 3거래소 병합(중복매수 구멍 차단).
    · held_cost = 잔고 − baseline(사용자 기보유)의 종목별 투입원가(원화).
    · reservations = 원장의 in-flight/planned BUY **잔여수량 전부**. 같은 종목 여러
      계획을 리스트로 보존하고 부분체결 뒤 잔량도 버리지 않는다.
    · 잔고에 먼저 보인 보유의 sleeve도 원장 pos_key/sleeve로 귀속해 대사 전 B가
      A로 기본귀속되지 않게 한다.
    """
    from bot import ledger, ownership
    rows: dict[str, dict] = {}
    kr = kis.positions_detail("KR")
    if kr is None:
        return None
    for p in kr:
        rows.setdefault(p["code"], p)
    for excg in _US_EXCGS:
        us = kis.positions_detail("US", excg=excg)
        if us is None:
            return None
        for p in us:
            rows.setdefault(p["code"], p)
    held = {c: int(p["qty"]) for c, p in rows.items()}
    base = ownership.baseline() or set()
    held_cost = {c: float(p.get("buy_amt") or 0)
                 * (1.0 if p.get("market") == "KR" else fx)
                 for c, p in rows.items() if c not in base}
    fold = ledger._fold()
    try:
        recorded = kis_positions.load()
    except Exception:
        recorded = {}

    def order_sleeve(key: str, order: dict) -> str:
        tagged = str(order.get("sleeve") or "").upper()
        identity = str(order.get("pos_key") or key)
        return "B" if tagged == "B" or identity.startswith("sb:") else "A"

    held_sleeves = {
        code: ("B" if (recorded.get(code) or {}).get("sleeve") == "B" else "A")
        for code in held
    }
    # 실제 체결 징후가 있는 최신 BUY만 대사 전 보유 귀속의 진실로 쓴다. 미제출
    # planned/미체결 주문이 기존 A 보유종목을 B로 재태깅하면 슬리브 회계가 흔들린다.
    tagged_orders = sorted(
        fold.items(),
        key=lambda item: float(item[1].get("submitted_at")
                               or item[1].get("created_at") or 0))
    for key, order in tagged_orders:
        symbol = str(order.get("symbol") or "").upper()
        if symbol not in held or str(order.get("side") or "").upper() != "BUY":
            continue
        if str(order.get("state") or "") == "planned":
            continue
        before = order.get("hldg_before")
        try:
            confirmed = max(0, int(order.get("filled") or 0))
            before_qty = int(before or 0)
        except (TypeError, ValueError):
            continue
        if confirmed <= 0 and (
                before is None or int(held[symbol]) <= before_qty):
            continue                              # 미체결 주문 — 기존 귀속 유지
        held_sleeves[symbol] = order_sleeve(key, order)

    reservations: list[dict] = []
    for k, v in fold.items():
        if ((v.get("side") or "").upper() != "BUY"
                or v.get("state") not in (ledger._INFLIGHT | {"planned"})):
            continue
        s = str(v.get("symbol") or "").upper()
        if not s or (v.get("state") == "partial" and v.get("open") is False):
            continue
        try:
            intended = max(0, int(v.get("intended") or 0))
            confirmed = max(0, int(v.get("filled") or 0))
            if v.get("state") != "planned" and s in held:
                if v.get("hldg_before") is not None:
                    delta = max(0, int(held[s]) - int(v.get("hldg_before") or 0))
                    confirmed = max(confirmed, min(intended, delta))
                elif s not in recorded:            # 전환 전 원장 메타의 보수적 복구
                    confirmed = max(confirmed, min(intended, int(held[s])))
            q = max(0, intended - confirmed)
            px = float(v.get("price") or v.get("limit") or 0)
            mk = v.get("market") or kis.market_of_symbol(s)
            cost = q * px * (1.0 if mk == "KR" else fx)
        except (TypeError, ValueError):
            q, cost = 0, 0.0
        if q > 0:
            reservations.append({
                "key": k, "symbol": s, "qty": q, "cost": cost,
                "sleeve": order_sleeve(k, v)})
    return held, held_cost, reservations, _sold_today(fold), held_sleeves


def _partition(held_cost: dict, reservations: list[dict], sleeve: str,
               held_sleeves: dict[str, str]) -> tuple[int, float]:
    """슬리브별 distinct 포지션 수와 held+예약 원가. 동종목 예약은 모두 합산."""
    if isinstance(held_sleeves, set):             # 이전 호출 계약 호환
        held_sleeves = {
            code: ("B" if code in held_sleeves else "A") for code in held_cost}
    if isinstance(reservations, dict):             # 이전 테스트/도구 계약 호환
        reservations = [
            {"symbol": symbol, "cost": value[0], "sleeve": value[1]}
            for symbol, value in reservations.items()]
    codes = set()
    cost = 0.0
    for c, cst in held_cost.items():
        if held_sleeves.get(c, "A") == sleeve:
            codes.add(c)
            cost += cst
    for reservation in reservations:
        if reservation["sleeve"] == sleeve:
            codes.add(reservation["symbol"])
            cost += float(reservation["cost"])
    return len(codes), cost


def _shelf_cands(signals: list[dict]) -> list[dict]:
    """매물대 반등(B) 후보 — group='shelf'·**fresh=True**·진입/손절 유효.
    손익비 높은 순. freshness는 A와 동일하게 행 단위로도 요구한다 — 문서가
    신선해도 stale 행이 실행기로 넘어가면 '신선한 신호만 집행' 전제가 깨진다
    (Codex P1-2)."""
    c = [s for s in signals if s.get("group") == "shelf"
         and s.get("fresh") is True
         and "entry" in s and "stop" in s]     # 무효값은 input 게이트가 진단(P2)
    c.sort(key=lambda s: -float((s.get("shelf") or {}).get("rr") or 0))
    return c


def run_once(signals: list[dict], *, fx: float | None = None,
             excg_of: dict | None = None, reason: str = "미러진입",  # legacy 표시명 —
             # 원장 키/멱등성과 무관(표시·알림용). rename은 별도 cleanup PR.
             sleeve: str = "A", group: str = "now",
             seed_krw: float | None = None) -> list[dict]:
    """신선한 스캐너 신호를 KIS 시세·게이트로 직접 집행 시도.
    반환: 종목별 {code, gate, ok?, qty?, why}.

    sleeve/group: 'A'/'now'=전환확정(기본) · 'B'/'shelf'=매물대 반등(별도 예산).
    seed_krw: 이 슬리브 전용 SEED(None이면 execute_entry가 기본 BOT_SEED_KRW).
    fx: USD→KRW 환율. excg_of: {code: 거래소}.
    """
    # 명시적으로 전달된 환율은 그대로 검증한다 — `fx or 기본값`은 0을 조용히
    #   기본값으로 되살려 낡은 환율로 사이징한다(Codex V2 P2). None만 기본값.
    raw_fx = settings.FX_USDKRW if fx is None else fx
    try:
        fx = float(raw_fx)
    except (TypeError, ValueError):
        return [{"code": "*", "gate": "input", "why": "환율 형식 오류"}]
    if not math.isfinite(fx) or fx <= 0:
        return [{"code": "*", "gate": "input", "why": "환율 무효(NaN·inf·0·음수)"}]
    excg_of = excg_of or {}
    results: list[dict] = []

    src = _shelf_cands(signals) if group == "shelf" else _now_signals(signals)
    # 1차 게이트(브로커 조회 전) — 세션·어닝. 후보가 없으면 잔고 조회도 안 한다.
    cand: list[dict] = []
    for s in src:
        code = str(s["code"]).upper()
        if not settings.market_open(s.get("ccy", "USD")):
            results.append({"code": code, "gate": "session", "why": "장 아님"})
            continue
        ed = s.get("earnings_d")
        try:
            ed = float(ed) if ed is not None else None
        except (TypeError, ValueError):
            ed = None
        if ed is not None and 0 <= ed <= 3:        # 공유 전략규칙: 어닝 D-3(갭 리스크)
            results.append({"code": code, "gate": "earnings",
                            "why": f"어닝 D-{int(ed)} 이내 — 신규 진입 금지"})
            continue
        cand.append(s)
    if not cand:
        return results

    st = _broker_state(fx)
    if st is None:                                 # 잔고 불명 → 보수적으로 안 산다
        for s in cand:
            results.append({"code": str(s["code"]).upper(), "gate": "holdings",
                            "why": "잔고 조회실패/불완전 — skip"})
        return results
    held, held_cost, reservations, sold_today, held_sleeves = st
    #   슬리브별 파티션 — A/B가 서로의 종목 수·투입원가를 세지 않게(예산 잠식 방지).
    n_open, open_cost = _partition(
        held_cost, reservations, sleeve, held_sleeves)
    total_open_cost = (sum(float(v) for v in held_cost.values())
                       + sum(float(r["cost"]) for r in reservations))
    total_held_cost = sum(float(v) for v in held_cost.values())
    sleeve_held_cost = sum(
        float(cost) for code, cost in held_cost.items()
        if held_sleeves.get(code, "A") == sleeve)
    operating_limit = envelope.operating_limit_krw()
    prefix = "sb:" if sleeve == "B" else "kb:"

    for s in cand:
        code = str(s["code"]).upper()
        ccy = s.get("ccy", "USD")
        market = kis.market_of_ccy(ccy)
        excg = excg_of.get(code, "NASD")

        if code in held:                           # 브로커-진실: 이미 보유 = 중복 금지(A·B 공통)
            results.append({"code": code, "gate": "already",
                            "why": f"이미 KIS 보유 {held[code]}주"}); continue
        if code in sold_today:                     # 당일 손절 종목 재진입 금지(패리티)
            results.append({"code": code, "gate": "cooldown",
                            "why": "당일 매도 종목 — 재진입 쿨다운"}); continue
        cur = kis.last_price(code, market=market, excg=excg)
        try:
            cur = float(cur) if cur is not None else None
        except (TypeError, ValueError):
            cur = None
        # NaN은 `not cur`도 `cur <= 0`도 False — 명시적 isfinite 없이는 NaN
        #   시세가 실행기까지 흘러 사이클 예외를 낸다(Codex P2).
        if cur is None or not math.isfinite(cur) or cur <= 0:
            results.append({"code": code, "gate": "quote", "why": "현재가 조회 실패"}); continue
        try:
            entry = float(s["entry"])
            stop = float(s["stop"])
        except (TypeError, ValueError):
            results.append({"code": code, "gate": "input",
                            "why": "진입/손절가 형식 오류"}); continue
        tactic = s.get("tactic") or {}
        raw_mode = (tactic.get("mode") if isinstance(tactic, dict) else tactic)
        mode = str(raw_mode or "full").strip().lower()
        if mode not in ("full", "half", "pullback"):
            # 알 수 없는 전술명은 tolerance·눌림가 검사를 전부 우회해 현재가
            #   주문으로 폴백했다(Codex V3 P1 — entry에서 100% 이탈해도 sent).
            #   공백·대소문자만 정규화하고 허용 집합 밖은 주문 전에 차단한다.
            results.append({"code": code, "gate": "tactic",
                            "why": f"알 수 없는 진입 전술({mode})"}); continue
        try:
            pb = float(tactic.get("pb_price") or 0) if isinstance(tactic, dict) else 0.0
        except (TypeError, ValueError):
            pb = 0.0
        if not (math.isfinite(entry) and math.isfinite(stop)
                and math.isfinite(pb)) or entry <= 0 or stop <= 0:
            # NaN·inf 손절/진입은 아래 부등식 게이트를 전부 미끄러져 통과하고
            #   (nan<=0 == False), 음수 stop은 per_share가 오히려 큰 양수가
            #   되어 sent까지 간다(Codex V2 P1 — 체결되면 회계·보호원장이
            #   stop<=0을 거부해 무보호 실보유가 남는다). 명시적으로 닫는다.
            results.append({"code": code, "gate": "input",
                            "why": "진입/손절가 무효(NaN·inf·0·음수)"}); continue
        if mode in ("full", "half") and abs(cur - entry) / entry > settings.ENTRY_TOLERANCE:
            results.append({"code": code, "gate": "tolerance",
                            "why": f"가격 괴리 {cur} vs 진입 {entry}"}); continue
        if mode in ("half", "pullback") and not (stop < pb < entry):
            results.append({"code": code, "gate": "tactic",
                            "why": f"{mode} 눌림가 무효({pb})"}); continue
        order_px = pb if mode == "pullback" else cur
        per_share = order_px - stop
        if per_share <= 0:
            results.append({"code": code, "gate": "input", "why": "손절폭 무효"}); continue

        pos_key = f"{prefix}{s.get('id') or code}"
        opened = settings.today_kst()
        tgt = None
        try:
            tgt = float(s.get("target") or 0)
        except (TypeError, ValueError):
            tgt = 0.0
        # NaN target은 truthy라 `or None` 정규화를 통과해 원장·포지션 메타로
        #   전파되고, 목표가 비교(NaN 비교=False)가 청산을 조용히 끈다(Codex P2).
        tgt = tgt if math.isfinite(tgt) and tgt > 0 else None
        order_meta = {"pos_key": pos_key, "sleeve": sleeve, "stop": stop,
                      "target": tgt, "name": s.get("name", ""), "opened": opened,
                      "tactic": mode, "pending": mode == "pullback"}
        d = kis_buy.execute_entry(pos_key, code, price_usd=order_px,
                                  per_share_risk_usd=per_share, krw_per_usd=fx,
                                  excg=excg, market=market, reason=reason,
                                  open_positions=n_open, open_cost_krw=open_cost,
                                  total_open_cost_krw=total_open_cost,
                                  held_cost_krw=sleeve_held_cost,
                                  total_held_cost_krw=total_held_cost,
                                  operating_limit_krw=operating_limit,
                                  hldg_before=0, seed_krw=seed_krw,
                                  sleeve=sleeve,
                                  limit_price=pb if mode == "pullback" else None,
                                  qty_fraction=0.5 if mode == "half" else 1.0,
                                  order_meta=order_meta)
        if d.ok:
            # ack는 체결이 아니다. 보호 포지션·원가장부는 대사가 실체결을 확인한 뒤
            # kis_accounting이 만든다. half의 잔여만 원장에 영속 계획으로 예약한다.
            pending_qty = 0
            if mode == "half":
                pending_qty = max(0, int(d.planned_qty or d.qty) - int(d.qty))
                if pending_qty > 0:
                    kis_pending.create_half_plan(
                        pos_key + ":pb", code, pending_qty, parent_key=pos_key,
                        limit=pb, stop=stop, market=market, excg=excg, fx=fx,
                        sleeve=sleeve, meta=order_meta)
            reserve_qty = int(d.planned_qty or d.qty)
            held[code] = reserve_qty
            n_open += 1
            reserve_px = ((d.qty * order_px + pending_qty * pb) / reserve_qty
                          if reserve_qty > 0 else order_px)
            open_cost += reserve_qty * reserve_px * (1.0 if market == "KR" else fx)
            total_open_cost += reserve_qty * reserve_px * (
                1.0 if market == "KR" else fx)
            held_sleeves[code] = sleeve
            if sleeve == "B":
                held_sleeves[code] = "B"
            try:
                from bot import notify
                u = "원" if market == "KR" else "$"
                tag = " (매물대B)" if sleeve == "B" else ""
                notify.send(
                    f"🟢 <b>KIS 매수 주문 접수{tag}</b> — {s.get('name', code)}({code})\n"
                    f"  전술 {mode} · 1차 {d.qty}주"
                    + (f" · 눌림대기 {pending_qty}주 @ {pb}{u}" if pending_qty else "")
                    + f" · 손절 {s['stop']}{u}",
                    critical=True, category="trade")
            except Exception:
                pass
        results.append({"code": code, "gate": d.gate, "ok": d.ok,
                        "qty": d.qty, "planned_qty": d.planned_qty,
                        "tactic": mode, "why": d.why})
    return results


def _fetch_signals() -> list[dict]:
    """검증된 신호 소스를 선택한다.

    평상시에는 GitHub 피드가 우선이고, 명시적으로 fallback을 연 경우에만
    지연된 GitHub 피드를 Oracle 로컬 분석 결과로 대체한다. 두 소스가 모두
    낡거나 계약 검증에 실패하면 빈 목록을 돌려 신규매수를 fail-closed한다.
    """
    from bot import signal_feed

    selected = signal_feed.load_selected()
    print(
        f"[신호소스] {selected.get('source', 'none')}"
        f" · {selected.get('why', 'validated')}"
        f" · age={selected.get('age_min')}",
        flush=True,
    )
    return selected.get("signals") or []


def _cycle() -> None:
    # 부팅 대사 — 이 프로세스의 매매 게이트(kis_boot.trading_allowed)를 연다.
    #   파수꾼과 별개 프로세스라 매수 루프도 자체적으로 돌려야 boot 게이트가 열린다.
    #   UNKNOWN 0건이면 원장 읽기만(가벼움) + ack(접수)→체결 잔고대사도 함께 수행.
    #   실패해도 게이트 닫힌 채 진행(fail-closed).
    try:
        from bot import kis_boot
        kis_boot.boot_reconcile()
    except Exception as e:
        print(f"[부팅 대사 오류] {type(e).__name__}: {e}", flush=True)
    try:
        from bot import kis_accounting
        watch = kis_accounting.monitor_unaccounted_fills()
        if watch.get("pending"):
            print(f"[체결 회계 감시] 미회계 예약 {watch['pending']}건 · "
                  f"신규 알림 {len(watch.get('alerts') or [])}건", flush=True)
    except Exception as e:
        # 감시 실패가 주문 안전 게이트를 약화하지 않는다. 미회계 예약은 ledger가
        # 계속 보존하므로 초과지출 대신 신규매수 가용성만 낮아진다.
        print(f"[체결 회계 감시 오류] {type(e).__name__}: {e}", flush=True)
    try:
        for p in kis_pending.process():
            print(f"  [대기] {p.get('key')} {p.get('act')} {p.get('why','')}",
                  flush=True)
    except Exception as e:
        print(f"[대기 주문 오류] {type(e).__name__}: {e}", flush=True)
    sigs = _fetch_signals()
    print(f"신호 {len(sigs)}건 로드 · 'now' {len(_now_signals(sigs))}건 · "
          f"'shelf' {len(_shelf_cands(sigs))}건", flush=True)
    for r in run_once(sigs):                          # 슬리브 A(전환확정)
        mark = "✓ 전송" if r.get("ok") else "·"
        print(f"  {mark} {r['code']} [{r['gate']}] {r.get('why', '')}", flush=True)
    # 슬리브 B(매물대 반등) — BOT_SEED_SB_KRW 설정 시에만(별도 예산 테스트).
    sb = envelope.sleeve_limit_krw("B")
    if sb > 0:
        for r in run_once(sigs, sleeve="B", group="shelf", seed_krw=sb,
                          reason="매물대B"):
            mark = "✓ 전송" if r.get("ok") else "·"
            print(f"  [B] {mark} {r['code']} [{r['gate']}] {r.get('why', '')}",
                  flush=True)
    # 성과 vs 지수 추적(알파) — 실패해도 매매에 영향 0(무해).
    try:
        from bot import alpha
        alpha.tick()
    except Exception as e:
        print(f"[알파 추적 오류] {type(e).__name__}: {e}", flush=True)


def main() -> int:
    import argparse
    import time
    try:
        poll_default = int(os.environ.get("BUYLOOP_POLL_SECONDS", "60"))
    except ValueError:
        poll_default = 60
    poll_default = max(10, min(300, poll_default))
    ap = argparse.ArgumentParser(description="KIS 미러 매수 루프(기본 1회)")
    ap.add_argument("--loop", action="store_true", help="POLL초마다 반복(서버 모드)")
    ap.add_argument("--poll", type=int, default=poll_default,
                    help=f"반복 주기(초, 기본 {poll_default}; 10초 미만 금지)")
    args = ap.parse_args()
    if args.poll < 10:
        raise SystemExit("--poll은 KIS 호출 경합 방지를 위해 10초 이상이어야 합니다")
    if not args.loop:
        _cycle()
        return 0
    print(f"매수 루프 시작 — {args.poll}초 주기(ALLOW_BUY·KIS_ORDERS_ENABLED 필요)",
          flush=True)
    while True:
        try:
            _cycle()
        except Exception as e:                     # 루프는 죽지 않는다
            print(f"[오류] {type(e).__name__}: {e}", flush=True)
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
