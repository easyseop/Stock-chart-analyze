"""KIS UNKNOWN 대사 — nccs(미체결)/ccnl(체결내역) 응답에서 유실 주문을 되찾는다.

문제(리뷰 '가장 위험한 델타 1위'): KIS엔 클라 멱등키가 없고 ODNO는 서버 채번이라,
주문 요청이 타임아웃되면 **그 주문이 나갔는지조차 모른다(UNKNOWN)**. 그대로 재주문
하면 이중주문/초과매도. 이 모듈은 그 복구 경로를 담당한다:

  1) 우리 원장의 UNKNOWN 주문(제출 시점에 남긴 합성키·의도 수량·시각)과
  2) 브로커 조회(nccs+ccnl 둘 다 — 완전체결은 nccs에 안 뜬다)의 행들을
  대조해 후보를 귀속시키고, ledger.reconcile_from_candidates로 신뢰도 판정:
    · 후보 1개  → HIGH: 자동 확정(체결량 반영·잠금 해제)
    · 후보 0/2+ → LOW : 잠금 유지(수동 검토) — 오매칭 초과매도 방지

읽기·계산 전용 — 이 모듈은 주문을 내지 않는다. 안전 원칙:
  · 우리가 아는(ODNO 결속된) 주문의 행은 후보에서 제외 — 남는 행만 유실 후보.
  · ODNO가 이미 있으면 합성키 매칭 대신 ODNO 직접 매칭(결정적).
  · 시간 윈도우(기본 ±120초) 밖 행은 후보 제외.

응답 필드(공식 샘플·python-kis 매핑 기준, 실측 대조 예정):
  nccs output:  odno, pdno, ft_ord_qty(주문수량), nccs_qty(미체결수량),
                ft_ccld_qty(체결수량, 있을 때), sll_buy_dvsn_cd(01매도/02매수), ord_tmd
  ccnl output:  odno, pdno, ft_ord_qty, ft_ccld_qty, ft_ccld_unpr3(체결단가),
                sll_buy_dvsn_cd(_name), ord_dt, ord_tmd
  ※ 매도/매수 코드(01/02)는 [대조필요] — 이름 필드(sll_buy_dvsn_cd_name)가 있으면
    '매도'/'매수' 문자열을 우선 신뢰한다(코드 뒤집힘 방어).
"""
from __future__ import annotations

from bot import ledger

# 매도/매수 구분 — 코드와 이름 둘 다 본다(코드 정의 [대조필요] 방어)
_SELL_CODES = {"01"}
_BUY_CODES = {"02"}


def _side_of(row: dict) -> str | None:
    """행의 매매 방향 'SELL'/'BUY'/None. 이름 필드 우선, 코드 폴백."""
    name = str(row.get("sll_buy_dvsn_cd_name") or row.get("sll_buy_dvsn_name") or "")
    if "매도" in name:
        return "SELL"
    if "매수" in name:
        return "BUY"
    code = str(row.get("sll_buy_dvsn_cd") or "")
    if code in _SELL_CODES:
        return "SELL"
    if code in _BUY_CODES:
        return "BUY"
    return None


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _tmd_seconds(tmd: str) -> int | None:
    """HHMMSS → 자정 기준 초. 파싱 불가면 None(시간 필터 미적용)."""
    s = str(tmd or "").strip()
    if len(s) != 6 or not s.isdigit():
        return None
    return int(s[:2]) * 3600 + int(s[2:4]) * 60 + int(s[4:6])


def normalize_rows(nccs: dict | None, ccnl: dict | None) -> list[dict]:
    """nccs/ccnl 응답을 공통 행 형태로 정규화(+중복 ODNO 병합 — ccnl 우선).

    반환 행: {odno, pdno, side, ord_qty, filled, price, ord_tmd, src}
    · filled: ccnl은 ft_ccld_qty, nccs는 ft_ord_qty−nccs_qty(부분체결 유추).
    · 같은 ODNO가 양쪽에 있으면 ccnl(체결 확정치)로 병합.
    """
    out: dict[str, dict] = {}

    def _norm(row: dict, src: str) -> dict | None:
        odno = str(row.get("odno") or "").strip()
        pdno = str(row.get("pdno") or "").strip()
        if not pdno and not odno:
            return None
        ord_qty = _f(row.get("ft_ord_qty"))
        still_open = False
        if src == "ccnl":
            filled = _f(row.get("ft_ccld_qty"))
        else:
            nq = _f(row.get("nccs_qty"), default=-1.0)
            cq = _f(row.get("ft_ccld_qty"), default=-1.0)
            filled = cq if cq >= 0 else (max(0.0, ord_qty - nq) if nq >= 0 else 0.0)
            still_open = nq > 0            # nccs 잔량>0 = 주문 아직 살아있음(체결 대기)
        return {"odno": odno, "pdno": pdno, "side": _side_of(row),
                "ord_qty": int(round(ord_qty)), "filled": int(round(filled)),
                "price": _f(row.get("ft_ccld_unpr3") or row.get("ft_ord_unpr3")),
                "ord_tmd": str(row.get("ord_tmd") or ""), "src": src,
                "open": still_open}   # 감사 수정 #6: 살아있는 주문은 잔여 재발주 금지

    for src, d in (("nccs", nccs), ("ccnl", ccnl)):
        for row in ((d or {}).get("output") or []):
            n = _norm(row, src)
            if not n:
                continue
            k = n["odno"] or f"{src}:{id(row)}"
            if k in out and src == "ccnl":
                out[k].update({kk: n[kk] for kk in
                               ("filled", "price", "src") if n[kk] or kk == "src"})
            else:
                out.setdefault(k, n)
    return list(out.values())


def normalize_domestic_rows(nccs: dict | None, ccnl: dict | None) -> list[dict]:
    """국내 정정취소가능/일별체결 응답을 해외와 같은 공통 행으로 정규화."""
    out: dict[str, dict] = {}

    def rows_of(d):
        return ((d or {}).get("output1") or (d or {}).get("output") or [])

    def norm(row: dict, src: str) -> dict | None:
        odno = str(row.get("odno") or row.get("ODNO") or "").strip()
        pdno = str(row.get("pdno") or row.get("PDNO") or "").strip().upper()
        if not odno and not pdno:
            return None
        oq = int(round(_f(row.get("ord_qty") or row.get("ORD_QTY"))))
        filled = int(round(_f(row.get("tot_ccld_qty") or row.get("ccld_qty")
                              or row.get("ft_ccld_qty"))))
        rem = _f(row.get("rmn_qty") or row.get("nccs_qty")
                 or row.get("ord_psbl_qty"), default=-1.0)
        if src == "nccs" and filled <= 0 and rem >= 0:
            filled = max(0, oq - int(round(rem)))
        return {"odno": odno, "pdno": pdno, "side": _side_of(row),
                "ord_qty": oq, "filled": filled,
                "price": _f(row.get("avg_prvs") or row.get("avg_prc")
                            or row.get("ccld_unpr") or row.get("ord_unpr")),
                "ord_tmd": str(row.get("ord_tmd") or ""),
                "src": "kr-" + src, "open": src == "nccs" and rem != 0}

    for src, d in (("nccs", nccs), ("ccnl", ccnl)):
        for row in rows_of(d):
            n = norm(row, src)
            if not n:
                continue
            k = n["odno"] or f"{src}:{id(row)}"
            if k in out and src == "ccnl":
                out[k].update({kk: n[kk] for kk in
                               ("filled", "price", "src") if n[kk] or kk == "src"})
                out[k]["open"] = False
            else:
                out.setdefault(k, n)
    return list(out.values())


def resolve_acks_from_rows(rows: list[dict]) -> list[dict]:
    """ODNO가 결속된 ack/partial 주문을 실제 체결행으로 갱신하고 즉시 회계한다."""
    from bot import kis_accounting
    results = []
    by_odno: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("odno"):
            by_odno.setdefault(str(row["odno"]), []).append(row)
    for o in ledger.open_orders():
        if o.get("state") not in ("submitted", "ack", "partial", "cancel_pending"):
            continue
        odno = str(o.get("odno") or "")
        symbol = str(o.get("symbol") or "").upper()
        side = str(o.get("side") or "").upper()
        matches = [r for r in by_odno.get(odno, [])
                   if str(r.get("pdno") or "").upper() == symbol
                   and (not r.get("side") or not side or r.get("side") == side)]
        if not odno or len(matches) != 1:
            continue                              # 모호하면 잔고 대사로도 자동 귀속하지 않음
        row = matches[0]
        intended = int(o.get("intended") or 0)
        filled = min(intended, max(0, int(row.get("filled") or 0)))
        price = float(row.get("price") or 0) or None
        opened = bool(row.get("open"))
        if filled >= intended and intended > 0:
            r = ledger.reconcile(o["key"], filled, fill_price=price,
                                 fill_price_source=str(row.get("src") or "broker"),
                                 open_order=False)
        elif filled > 0:
            ledger.on_result(o["key"], "partial", filled, fill_price=price,
                             fill_price_source=str(row.get("src") or "broker"),
                             open_order=opened)
            r = {"state": "partial", "filled": filled,
                 "residual": max(0, intended - filled), "fill_price": price,
                 "open": opened}
        elif not opened:
            ledger.on_result(o["key"], "rejected", 0, open_order=False)
            r = {"state": "rejected", "filled": 0, "residual": intended,
                 "fill_price": None, "open": False}
        else:
            continue
        acct = kis_accounting.sync_fill(
            o["key"], filled_qty=filled, fill_price=price,
            fill_price_source=str(row.get("src") or "broker"))
        results.append({"key": o["key"], "symbol": o.get("symbol"),
                        "side": o.get("side"), "market": o.get("market"),
                        "via": "broker-fills", "accounting": acct, **r})
    return results


def candidates_for(unknown: dict, rows: list[dict], known_odnos: set[str],
                   window_s: int = 120) -> list[dict]:
    """UNKNOWN 주문 1건의 후보 행들. unknown: 원장 fold 항목
    {symbol, intended, odno?, ord_tmd?, side?(meta)}.

    귀속 규칙(보수적 — 오매칭이 초과매도보다 나쁘므로 좁게):
      · ODNO가 결속돼 있으면 그 ODNO 행만(결정적).
      · 아니면: 같은 종목 ∧ (side 알면 같은 방향) ∧ 주문수량==의도수량 ∧
        이미 다른 주문에 결속된 ODNO 제외 ∧ (둘 다 시각 있으면 ±window_s 이내).
    """
    odno = str(unknown.get("odno") or "")
    if odno:
        return [r for r in rows if r["odno"] == odno]
    symbol = unknown.get("symbol")
    side = (unknown.get("side") or "").upper() or None
    intended = int(unknown.get("intended", 0))
    t0 = _tmd_seconds(unknown.get("ord_tmd") or "")
    out = []
    for r in rows:
        if r["pdno"] != symbol:
            continue
        if r["odno"] and r["odno"] in known_odnos:
            continue                              # 이미 귀속된(우리가 아는) 주문
        if side and r["side"] and r["side"] != side:
            continue
        if intended > 0 and r["ord_qty"] != intended:
            continue
        t1 = _tmd_seconds(r["ord_tmd"])
        if t0 is not None and t1 is not None and abs(t1 - t0) > window_s:
            continue
        out.append(r)
    return out


def _kr_holdings(balance: dict | None) -> tuple[dict | None, bool]:
    """국내 잔고(output1) → {pdno.upper(): hldg_qty 합}. (맵|None, complete).
    · 조회 실패/rt_cd≠0/파싱불가 → (None, False): 신뢰 불가.
    · 연속조회 키(ctx_area_nk100)가 남아 있으면 complete=False(불완전 → 심볼 부재를
      '0주'로 오해하면 초과매도 → 불완전이면 대사 보류). 봇 계좌는 종목 少라 보통 1페이지."""
    if not balance or balance.get("rt_cd") != "0":
        return None, False
    complete = not str(balance.get("ctx_area_nk100")
                       or balance.get("CTX_AREA_NK100") or "").strip()
    hmap: dict[str, int] = {}
    for r in (balance.get("output1") or []):
        sym = str(r.get("pdno") or "").upper()
        if not sym:
            continue
        try:
            hmap[sym] = hmap.get(sym, 0) + int(float(r.get("hldg_qty") or 0))
        except (TypeError, ValueError):
            return None, False                    # 파싱불가 = 신뢰 불가(추측 금지)
    return hmap, complete


def reconcile_unknowns_kr(balance: dict | None) -> list[dict]:
    """국내(KR) UNKNOWN 대사 — 잔고 delta 기반(국내 nccs 모의 미지원·costbook 미배선).

    **안전 정리(초과매도·이중주문 구조적 봉쇄)**: KR UNKNOWN은 오직 unknown→filled로만,
    잔고가 '정확한 full-fill'을 증명할 때만 전이한다. '미체결/부분/거부'는 절대 자동
    결론하지 않는다 → 미해소 UNKNOWN은 잠금(is_locked)이 유지돼 재주문이 원천 차단된다.
    따라서 잘못돼도 최악은 '수동검토로 남김'이지, 재매도가 아니다.

    기준(before): 제출 시 meta.hldg_before(파수꾼이 그 시점 보유수량 기록 — 별도 API
    호출 없이 손절 핫패스 유지). delta = before − 현재잔고. side는 명시 meta.side만 신뢰.

    fail-closed 게이트(하나라도 걸리면 LOW=잠금 유지):
      G0 잔고 조회실패/불완전, G1 SELL 아님/불명·intended≤0(BUY는 항상 수동),
      G2 심볼 non-terminal 주문 ≠1(net 귀속 불가), G3 ownership 미armed/기보유/동결,
      G4 before 스냅샷 없음, G5 before<Q(impossible sell)→동결, G6 delta≠정확Q → LOW
      (delta<0·delta>Q 등 이상치는 동결).
    반환: reconcile_unknowns와 동일 형태(KR 항목만) + kr_reason.
    """
    from bot import kis, ownership
    fold_open = ledger.open_orders()

    def _is_kr(o: dict) -> bool:
        return (o.get("market") == "KR"
                or kis.market_of_symbol(o.get("symbol", "")) == "KR")

    kr = [o for o in fold_open
          if o.get("state") == "unknown" and not o.get("reconciled") and _is_kr(o)]
    if not kr:
        return []

    hmap, complete = _kr_holdings(balance)
    open_count: dict[str, int] = {}
    for o in fold_open:                            # 심볼별 non-terminal 주문 수
        s = str(o.get("symbol") or "").upper()
        open_count[s] = open_count.get(s, 0) + 1

    results = []

    def _low(o, reason):
        # 이미 LOW로 판정된 UNKNOWN은 원장에 confidence 재기록 안 함(매cycle 무한증가
        #   방지) + already_low 표시(호출부가 재알림 억제 — 알림 폭주 방지).
        already = o.get("confidence") == ledger.CONF_LOW
        if not already:
            ledger.reconcile_from_candidates(o["key"], [], intended=o.get("intended"))
        intended = int(o.get("intended") or 0)
        filled = int(o.get("filled") or 0)
        results.append({"key": o["key"], "symbol": o.get("symbol"),
                        "candidates": 0, "kr_reason": reason,
                        "confidence": ledger.CONF_LOW, "state": "unknown",
                        "filled": filled, "residual": max(0, intended - filled),
                        "already_low": already})

    def _freeze_once(S, why):
        if not ownership.is_frozen(S):             # 이미 동결이면 재알림 금지
            ownership.freeze(S, why)

    for o in kr:
        try:
            S = str(o.get("symbol") or "").upper()
            Q = int(o.get("intended") or 0)
            side = (o.get("side") or "").upper()
            if hmap is None or not complete:
                _low(o, "잔고 조회실패/불완전"); continue
            if side != "SELL" or Q <= 0:
                _low(o, "BUY/side불명 — 자동해소 금지(수동)"); continue   # BUY 항상 LOW
            if open_count.get(S, 0) != 1:
                _low(o, f">1 non-terminal 주문({open_count.get(S,0)}) — net 귀속불가"); continue
            b = ownership.baseline()
            if b is None or S in b or ownership.is_frozen(S):
                _low(o, "ownership 미armed/기보유/동결"); continue
            before = o.get("hldg_before")
            if before is None:
                _low(o, "before 스냅샷 없음"); continue
            before = int(before)
            now = int(hmap.get(S, 0))              # complete 확인됨 → 부재=0주 신뢰
            if Q > before:                         # 보유보다 많이 매도? 설명불가
                _freeze_once(S, f"국내대사 impossible sell Q={Q}>before={before}")
                _low(o, f"impossible sell Q>{before} → 동결"); continue
            delta = before - now
            if delta == Q:                         # 정확 full-fill만 자동확정
                r = ledger.reconcile_from_candidates(
                    o["key"], [{"filled": Q, "odno": o.get("odno") or ""}], intended=Q)
                results.append({"key": o["key"], "symbol": o.get("symbol"),
                                "candidates": 1, "kr_reason": "잔고확정 full SELL", **r})
            elif delta < 0 or delta > Q:           # 증가/과다감소 = 외부개입 의심
                _freeze_once(S, f"국내대사 이상 delta={delta} (before={before} now={now} Q={Q})")
                _low(o, f"이상 delta={delta} → 동결")
            else:                                  # 0≤delta<Q: 부분/미체결 — 자동해소 금지
                _low(o, f"부분/미체결 delta={delta}<Q — 수동")
        except Exception as e:                     # 손상 줄 1건이 배치를 깨지 않게
            _low(o, f"처리 예외({type(e).__name__}) — 보수적 LOW")
    return results


ACK_AGE_MIN_S = 90     # ack 해소 최소 나이(초) — 체결 진행 중 잔고 스냅샷 오독 방지


def _verified_migrated_baseline_sell(order: dict, symbol: str,
                                     before: int) -> bool:
    """baseline 예외는 durable하게 이관된 봇 lot의 SELL에만 허용한다.

    baseline 전체를 풀면 사용자 보유의 잔고 감소를 봇 주문 체결로 오귀속할 수 있다.
    포지션 정체성·수량과 원가 lot이 모두 정확히 일치할 때만 좁게 통과시킨다.
    """
    if str(order.get("side") or "").upper() != "SELL":
        return False
    pos_key = str(order.get("pos_key") or "")
    if not pos_key or before <= 0:
        return False
    try:
        from bot import costbook, kis_positions
        rec = kis_positions.load().get(symbol) or {}
        book = costbook._fold()
        lot = (book.get("lots") or {}).get(pos_key) or {}
        return bool(
            book.get("healthy")
            and rec.get("legacy_migrated") is True
            and str(rec.get("pos_key") or "") == pos_key
            and int(rec.get("qty") or 0) == before
            and str(lot.get("symbol") or "").upper() == symbol
            and int(lot.get("qty") or 0) == before
        )
    except Exception:
        return False


def resolve_acks_by_balance(hmaps: dict[str, dict | None],
                            now_ts: float | None = None,
                            fill_prices: dict[str, dict[str, float]] | None = None,
                            only_keys: set[str] | None = None,
                            *,
                            complete_snapshot: bool = False,
                            realized_days: dict[str, str] | None = None,
                            ) -> list[dict]:
    """접수(submitted/ack) 주문의 잔고-delta 확정 — KR unknown 대사와 동일한 안전 정리.

    문제(2026-07-15 검토, 치명): KIS 주문응답은 '접수(ack)'까지만 온다 — 체결 통지
    채널이 없고(모의: nccs/psamount 미지원) ack를 filled로 바꿔주는 코드가 없어
    ack가 원장에 **영원히** 남는다. 그 결과:
      ① 파수꾼이 그 종목 손절을 영원히 스킵(open_order_count≥1 → 재발주 금지)
         = 매수 루프로 산 포지션 전부 **무보호**.
      ② can_submit이 같은 종목 재진입을 영구 차단(청산 후에도).
      ③ 미러 캡(n_open)이 in-flight로 영구 인플레이트 → 슬롯 고갈.
    이 함수가 잔고로 '정확한 full-fill'을 증명할 때만 ack→filled 전이해 셋 다 푼다.

    hmaps: {"KR": {sym: qty}|None, "US": {...}|None} — kis.holdings 결과
      (None=조회실패/불완전 → 그 시장 항목은 건드리지 않음).

    안전(전부 fail-closed — 하나라도 불충족이면 **그대로 둠**, LOW 강등·동결 외 부작용 없음):
      · age ≥ ACK_AGE_MIN_S (방금 낸 주문의 부분 체결 스냅샷 오독 방지)
      · side 명시(BUY/SELL) ∧ intended>0 ∧ meta.hldg_before 기록 존재
      · 그 심볼의 non-terminal 주문이 정확히 1건(net 귀속 가능)
      · ownership armed ∧ 심볼이 baseline(사용자 기보유)·동결 아님
      · delta(BUY: 현재−before / SELL: before−현재)가 intended와 **정확히 일치**
      · delta 이상치(음수/초과)는 동결+보류(외부 개입 의심 — unknown 대사와 동일)
    반환: 확정건만 [{key, symbol, side, market, state, filled, residual, via}].
    """
    import time as _t
    from bot import kis, kis_accounting, ownership
    # 잔고 map 일부만 주고 대상키도 제한하지 않으면, 누락 종목을 0주로 오인해
    # 관계없는 SELL을 체결로 확정할 수 있다. 전체 snapshot임을 호출자가 명시하거나
    # exact 주문키 집합을 함께 주는 두 계약만 허용한다.
    if only_keys is None and not complete_snapshot:
        return []
    now_ts = _t.time() if now_ts is None else float(now_ts)
    fold_open = ledger.open_orders()
    open_count: dict[str, int] = {}
    for o in fold_open:                            # 심볼별 non-terminal 주문 수
        s = str(o.get("symbol") or "").upper()
        open_count[s] = open_count.get(s, 0) + 1

    results: list[dict] = []
    base = ownership.baseline()
    for o in fold_open:
        if only_keys is not None and str(o.get("key") or "") not in only_keys:
            continue
        if o.get("state") not in ("submitted", "ack") or o.get("reconciled"):
            continue
        side = (o.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            continue                               # CANCEL 등 — 대상 아님
        S = str(o.get("symbol") or "").upper()
        Q = int(o.get("intended") or 0)
        before = o.get("hldg_before")
        if not S or Q <= 0 or before is None:
            continue                               # 기준 없음 — 자동확정 불가(보류)
        if now_ts - float(o.get("submitted_at") or 0) < ACK_AGE_MIN_S:
            continue                               # 아직 어림 — 다음 사이클
        if open_count.get(S, 0) != 1:
            continue                               # net 귀속 불가(동시 주문) — 보류
        if base is None or ownership.is_frozen(S):
            continue                               # 미armed/동결 — 보류
        # 구버전 절반익절은 hldg_before에 전체 보유가 아니라 주문수량을 잘못
        # 저장했다. 이관 도구가 durable pos_key+원가 lot+원래 수량을 모두 증명한
        # 주문만 legacy_hldg_before로 교정한다. 일반 주문은 기존 before 그대로다.
        legacy_before = o.get("legacy_hldg_before")
        if legacy_before is not None:
            try:
                legacy_before = int(legacy_before)
            except (TypeError, ValueError):
                continue
            if _verified_migrated_baseline_sell(o, S, legacy_before):
                before = legacy_before
        if S in base and not _verified_migrated_baseline_sell(
                o, S, int(before)):
            continue                               # 사용자 기보유는 검증된 이관 SELL만 예외
        market = o.get("market") or kis.market_of_symbol(S)
        hmap = hmaps.get(market)
        if hmap is None:
            continue                               # 그 시장 잔고 조회실패 — 보류
        now_qty = int(hmap.get(S, 0))
        before = int(before)
        delta = (now_qty - before) if side == "BUY" else (before - now_qty)
        if delta == Q:                             # 정확 full-fill만 자동확정
            # 보유 평단은 BUY 체결가의 근사 근거일 뿐 SELL 체결가가 아니다.
            # SELL에서 평단을 쓰면 실현손익이 0 근처로 조작되므로 제출가만 폴백한다.
            balance_price = ((fill_prices or {}).get(market) or {}).get(S)
            price = balance_price if side == "BUY" else None
            source = "balance-average" if price else "submitted-fallback"
            price = float(price or o.get("price") or 0) or None
            r = ledger.reconcile(o["key"], Q, fill_price=price,
                                 fill_price_source=source)
            acct = kis_accounting.sync_fill(
                o["key"], filled_qty=Q, fill_price=price,
                fill_price_source=source,
                realized_day_kst=(realized_days or {}).get(o["key"]))
            results.append({"key": o["key"], "symbol": S, "side": side,
                            "market": market, "via": "ack-balance",
                            "accounting": acct, **r})
        elif delta < 0 or delta > Q:               # 설명불가 변화 = 외부 개입 의심
            if not ownership.is_frozen(S):
                ownership.freeze(S, f"ack대사 이상 delta={delta} "
                                    f"(before={before} now={now_qty} Q={Q} {side})")
        # 0 ≤ delta < Q: 부분/미체결 가능성 — 자동 결론 금지(다음 사이클 재검사)
    return results


def reconcile_unknowns(nccs: dict | None, ccnl: dict | None,
                       window_s: int = 120) -> list[dict]:
    """원장의 모든 미해소 UNKNOWN을 nccs+ccnl로 대사(신뢰도 판정 포함).

    반환: [{key, symbol, confidence, state, filled, residual, candidates}].
    HIGH만 자동 확정되고(잠금 해제), LOW는 잠금 유지 — 호출부는 LOW를 P0로 알릴 것.
    """
    from bot import kis, kis_accounting
    rows = normalize_rows(nccs, ccnl)
    fold_open = ledger.open_orders()
    # 이미 결속된 ODNO들 — 다른 UNKNOWN의 후보에서 제외(교차 오귀속 방지)
    known = {str(o.get("odno")) for o in fold_open if o.get("odno")}
    results = []
    for o in fold_open:
        if o.get("state") != "unknown" or o.get("reconciled"):
            continue
        # 국내(KR)는 nccs/ccnl 필드가 달라 여기서 처리 금지 — reconcile_unknowns_kr 담당.
        if (o.get("market") == "KR"
                or kis.market_of_symbol(o.get("symbol", "")) == "KR"):
            continue
        key = o["key"]
        cands = candidates_for(o, rows, known_odnos=known - {str(o.get("odno") or "")},
                               window_s=window_s)
        r = ledger.reconcile_from_candidates(key, cands,
                                             intended=o.get("intended"))
        # 단일 후보에 ODNO가 있으면 (LOW라도) 결속 — 추적/취소 핸들 확보. 초과매도
        #   방지는 잠금이 담당(감사 수정 #6: 살아있는 주문은 확정 안 하고 잠금 유지).
        #   2개+ 모호는 결속 금지(오귀속 방지).
        if len(cands) == 1 and cands[0].get("odno"):
            ledger.bind_broker_order(key, cands[0]["odno"],
                                     ord_tmd=cands[0].get("ord_tmd", ""))
        if len(cands) == 1 and int(r.get("filled") or 0) > 0:
            r["accounting"] = kis_accounting.sync_fill(
                key, filled_qty=int(r["filled"]), fill_price=r.get("fill_price"),
                fill_price_source=str(cands[0].get("src") or "broker"))
        results.append({"key": key, "symbol": o.get("symbol"),
                        "candidates": len(cands), **r})
    return results
