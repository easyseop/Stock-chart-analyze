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

import os
import re

from bot import ledger

# 매도/매수 구분 — 코드와 이름 둘 다 본다(코드 정의 [대조필요] 방어)
_SELL_CODES = {"01"}
_BUY_CODES = {"02"}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def clean_broker_text(value, limit: int = 200) -> str:
    """브로커 메시지를 제어문자 없이 제한 길이로 정규화한다."""
    text = _CONTROL_RE.sub(" ", str(value or ""))
    return " ".join(text.split())[:max(0, int(limit))]


def trusted_response_rows(response: dict | None, *, domestic: bool = False
                          ) -> list[dict] | None:
    """부재 증명에 쓸 수 있는 완전한 단일 페이지 응답만 행으로 반환한다.

    빈 리스트는 성공한 부재 증거이고 ``None``은 조회 실패·형식 불신·연속조회
    미완이다. 두 값을 절대 합치지 않는다.
    """
    if not isinstance(response, dict) or response.get("rt_cd") != "0":
        return None
    suffix = "100" if domestic else "200"
    if str(response.get(f"ctx_area_nk{suffix}")
           or response.get(f"CTX_AREA_NK{suffix}") or "").strip():
        return None
    if "output1" in response:
        rows = response.get("output1")
    elif "output" in response:
        rows = response.get("output")
    else:
        return None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    return rows


def order_no_key(value) -> str:
    """KIS 숫자 주문번호의 선행 0 표현 차이를 제거한 비교키."""
    value = str(value or "").strip()
    if value.isdigit():
        return str(int(value))
    return value


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
        msg_cd = clean_broker_text(row.get("msg_cd") or row.get("rjct_rson"))
        msg1 = clean_broker_text(row.get("msg1") or row.get("rjct_rson_name")
                                 or row.get("prcs_stat_name"))
        status = clean_broker_text(row.get("prcs_stat_name")
                                   or row.get("rvse_cncl_dvsn_name"))
        return {"odno": odno, "pdno": pdno, "side": _side_of(row),
                "ord_qty": int(round(ord_qty)), "filled": int(round(filled)),
                "price": _f(row.get("ft_ccld_unpr3") or row.get("ft_ord_unpr3")),
                "ord_tmd": str(row.get("ord_tmd") or ""), "src": src,
                "open": still_open, "msg_cd": msg_cd, "msg1": msg1,
                "broker_status": status}

    for src, d in (("nccs", nccs), ("ccnl", ccnl)):
        for row in ((d or {}).get("output") or []):
            n = _norm(row, src)
            if not n:
                continue
            k = order_no_key(n["odno"]) or f"{src}:{id(row)}"
            if k in out and src == "ccnl":
                out[k].update({kk: n[kk] for kk in
                               ("filled", "price", "src", "msg_cd", "msg1",
                                "broker_status") if n[kk] or kk == "src"})
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
        msg_cd = clean_broker_text(row.get("msg_cd") or row.get("rjct_rson"))
        msg1 = clean_broker_text(row.get("msg1") or row.get("rjct_rson_name")
                                 or row.get("prcs_stat_name"))
        status = clean_broker_text(row.get("prcs_stat_name")
                                   or row.get("rvse_cncl_dvsn_name"))
        return {"odno": odno, "pdno": pdno, "side": _side_of(row),
                "ord_qty": oq, "filled": filled,
                "price": _f(row.get("avg_prvs") or row.get("avg_prc")
                            or row.get("ccld_unpr") or row.get("ord_unpr")),
                "ord_tmd": str(row.get("ord_tmd") or ""),
                "src": "kr-" + src, "open": src == "nccs" and rem != 0,
                "msg_cd": msg_cd, "msg1": msg1, "broker_status": status}

    for src, d in (("nccs", nccs), ("ccnl", ccnl)):
        for row in rows_of(d):
            n = norm(row, src)
            if not n:
                continue
            k = order_no_key(n["odno"]) or f"{src}:{id(row)}"
            if k in out and src == "ccnl":
                out[k].update({kk: n[kk] for kk in
                               ("filled", "price", "src", "msg_cd", "msg1",
                                "broker_status") if n[kk] or kk == "src"})
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
            by_odno.setdefault(order_no_key(row["odno"]), []).append(row)
    for o in ledger.open_orders():
        if o.get("state") not in ("submitted", "ack", "partial", "cancel_pending"):
            continue
        odno = order_no_key(o.get("odno"))
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
            r = ledger.reconcile(o["key"], 0, open_order=False)
            msg_cd = clean_broker_text(row.get("msg_cd"))
            msg1 = clean_broker_text(row.get("msg1"))
            status = clean_broker_text(row.get("broker_status"))
            broker_reason = msg1 or status or "사유 미상(브로커 종결 행)"
            ledger.record_reconcile_meta(
                o["key"], reason="broker-closed-zero-fill",
                meta={"source": str(row.get("src") or "broker"),
                      "msg_cd": msg_cd, "msg1": msg1,
                      "broker_reason": broker_reason,
                      "side": side, "intended": intended})
            r.update({"broker_reason": broker_reason,
                      "reason": "broker-closed-zero-fill"})
        else:
            continue
        acct = kis_accounting.sync_fill(
            o["key"], filled_qty=filled, fill_price=price,
            fill_price_source=str(row.get("src") or "broker"))
        results.append({"key": o["key"], "symbol": o.get("symbol"),
                        "side": o.get("side"), "market": o.get("market"),
                        "via": "broker-fills", "accounting": acct, **r})
    return results


REJECT_ABSENCE_MIN_S = int(
    os.environ.get("REJECT_ABSENCE_MIN_S", "600") or 600)


def resolve_acks_by_absence(evidence_by_key: dict[str, dict],
                            now_ts: float | None = None,
                            orders: list[dict] | None = None,
                            ) -> tuple[list[dict], list[dict]]:
    """미체결·체결 모두 부재이고 잔고가 불변인 오래된 ACK만 거절 종결한다.

    ``evidence_by_key``의 각 값은 호출자가 강한 응답 검증을 끝낸
    ``nccs_rows``/``ccnl_rows``(빈 list=성공, None=실패)와 완전한
    ``holdings`` map을 담는다. 하나라도 None이면 그 주문에는 어떤 이벤트도 쓰지
    않는다. 잔고가 바뀌었는데 두 주문조회에 행이 없는 모순은 자동정산하지 않고
    호출자가 경보할 수 있도록 별도로 반환한다.
    """
    import time as _time

    now_ts = _time.time() if now_ts is None else float(now_ts)
    open_orders = ledger.open_orders() if orders is None else list(orders)
    open_count: dict[str, int] = {}
    broker_inflight = {"submitted", "ack", "partial", "cancel_pending", "unknown"}
    for row in open_orders:
        if row.get("state") not in broker_inflight:
            continue
        if row.get("state") == "partial" and row.get("open") is False:
            continue
        symbol = str(row.get("symbol") or "").upper()
        open_count[symbol] = open_count.get(symbol, 0) + 1

    resolved: list[dict] = []
    contradictions: list[dict] = []
    for order in open_orders:
        key = str(order.get("key") or "")
        if order.get("state") not in ("submitted", "ack"):
            continue
        if now_ts - float(order.get("submitted_at") or 0) \
                < max(REJECT_ABSENCE_MIN_S, ACK_AGE_MIN_S):
            continue
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").upper()
        odno = order_no_key(order.get("odno"))
        intended = int(order.get("intended") or 0)
        before_raw = order.get("hldg_before")
        proof = evidence_by_key.get(key)
        if (not proof or side not in ("BUY", "SELL") or not odno
                or not symbol or intended <= 0 or before_raw is None
                or open_count.get(symbol) != 1):
            continue
        nccs_rows = proof.get("nccs_rows")
        ccnl_rows = proof.get("ccnl_rows")
        holdings = proof.get("holdings")
        if (nccs_rows is None or ccnl_rows is None or holdings is None
                or not isinstance(nccs_rows, list)
                or not isinstance(ccnl_rows, list)
                or not isinstance(holdings, dict)):
            continue
        if any(not isinstance(row, dict) for row in nccs_rows + ccnl_rows):
            continue

        def has_order(rows: list[dict]) -> bool:
            return any(order_no_key(row.get("odno") or row.get("ODNO")) == odno
                       for row in rows)

        if has_order(nccs_rows) or has_order(ccnl_rows):
            continue
        try:
            before = int(float(before_raw))
            current = int(float(holdings.get(symbol, 0)))
        except (TypeError, ValueError):
            continue
        unchanged = current == before
        if not unchanged:
            contradictions.append({
                "key": key, "symbol": symbol, "side": side,
                "intended": intended, "hldg_before": before,
                "hldg_now": current, "reason": "absence-balance-contradiction",
            })
            continue
        result = ledger.reconcile(key, 0, open_order=False)
        ledger.record_reconcile_meta(
            key, reason="absence-proof",
            meta={"source": "absence-proof", "nccs_count": 0,
                  "ccnl_count": 0, "hldg_before": before,
                  "hldg_now": current, "side": side,
                  "intended": intended,
                  "broker_reason": "사유 미상(부재 증명)"})
        resolved.append({"key": key, "symbol": symbol, "side": side,
                         "market": order.get("market"), "via": "absence-proof",
                         "broker_reason": "사유 미상(부재 증명)",
                         **result})
    return resolved, contradictions


def candidates_for(unknown: dict, rows: list[dict], known_odnos: set[str],
                   window_s: int = 120) -> list[dict]:
    """UNKNOWN 주문 1건의 후보 행들. unknown: 원장 fold 항목
    {symbol, intended, odno?, ord_tmd?, side?(meta)}.

    귀속 규칙(보수적 — 오매칭이 초과매도보다 나쁘므로 좁게):
      · ODNO가 결속돼 있으면 그 ODNO 행만(결정적).
      · 아니면: 같은 종목 ∧ (side 알면 같은 방향) ∧ 주문수량==의도수량 ∧
        이미 다른 주문에 결속된 ODNO 제외 ∧ (둘 다 시각 있으면 ±window_s 이내).
    """
    odno = order_no_key(unknown.get("odno"))
    if odno:
        return [r for r in rows if order_no_key(r.get("odno")) == odno]
    symbol = unknown.get("symbol")
    side = (unknown.get("side") or "").upper() or None
    intended = int(unknown.get("intended", 0))
    t0 = _tmd_seconds(unknown.get("ord_tmd") or "")
    out = []
    for r in rows:
        if r["pdno"] != symbol:
            continue
        if r["odno"] and order_no_key(r["odno"]) in known_odnos:
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
    # ``ledger.open_orders``에는 아직 브로커로 전송하지 않은 half 전술의 2차
    # ``planned`` leg도 포함된다. 잔고 delta 귀속의 모호성은 실제 브로커
    # in-flight끼리만 생기므로 planned까지 세면 1차 BUY가 체결돼도 영원히 ACK로
    # 남아 보호원장에 들어가지 못한다. ledger.open_order_count와 같은 상태 집합을
    # 사용해 전송 전 계획은 제외하되 실제 동시 주문 2건은 계속 보류한다.
    broker_inflight = {"submitted", "ack", "partial", "cancel_pending", "unknown"}
    for o in fold_open:
        if o.get("state") not in broker_inflight:
            continue
        if o.get("state") == "partial" and o.get("open") is False:
            continue
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
    broker_inflight = {"submitted", "ack", "partial", "cancel_pending", "unknown"}
    for o in fold_open:
        # half 전술의 2차 ``planned`` leg는 아직 브로커 주문이 아니므로 1차 ACK의
        # 잔고 delta 귀속을 모호하게 만들지 않는다. 실제 브로커 in-flight 2건은
        # 계속 open_count=2로 남아 자동대사를 보류한다.
        if o.get("state") not in broker_inflight:
            continue
        if o.get("state") == "partial" and o.get("open") is False:
            continue
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
        if base is None:
            continue                               # 미armed — 보류
        # 구버전 절반익절은 hldg_before에 전체 보유가 아니라 주문수량을 잘못
        # 저장했다. 이관 도구가 durable pos_key+원가 lot+원래 수량을 모두 증명한
        # 주문만 legacy_hldg_before로 교정한다. 일반 주문은 기존 before 그대로다.
        legacy_before = o.get("legacy_hldg_before")
        verified_migrated = False
        if legacy_before is not None:
            try:
                legacy_before = int(legacy_before)
            except (TypeError, ValueError):
                continue
            if _verified_migrated_baseline_sell(o, S, legacy_before):
                before = legacy_before
                verified_migrated = True
        if not verified_migrated:
            verified_migrated = _verified_migrated_baseline_sell(
                o, S, int(before))
        # 과거 잘못된 hldg_before 때문에 이미 동결된 legacy ACK도 이관의
        # 3중 증명(pos_key·보호수량·원가 lot)을 통과한 경우에만 해소한다.
        # 일반 동결/사용자 baseline 주문은 계속 fail-closed로 보류한다.
        if (ownership.is_frozen(S) or S in base) and not verified_migrated:
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
    known = {order_no_key(o.get("odno")) for o in fold_open if o.get("odno")}
    results = []
    for o in fold_open:
        if o.get("state") != "unknown" or o.get("reconciled"):
            continue
        # 국내(KR)는 nccs/ccnl 필드가 달라 여기서 처리 금지 — reconcile_unknowns_kr 담당.
        if (o.get("market") == "KR"
                or kis.market_of_symbol(o.get("symbol", "")) == "KR"):
            continue
        key = o["key"]
        cands = candidates_for(o, rows,
                               known_odnos=known - {order_no_key(o.get("odno"))},
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
