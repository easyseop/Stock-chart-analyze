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
        if src == "ccnl":
            filled = _f(row.get("ft_ccld_qty"))
        else:
            nq = _f(row.get("nccs_qty"), default=-1.0)
            cq = _f(row.get("ft_ccld_qty"), default=-1.0)
            filled = cq if cq >= 0 else (max(0.0, ord_qty - nq) if nq >= 0 else 0.0)
        return {"odno": odno, "pdno": pdno, "side": _side_of(row),
                "ord_qty": int(round(ord_qty)), "filled": int(round(filled)),
                "price": _f(row.get("ft_ccld_unpr3") or row.get("ft_ord_unpr3")),
                "ord_tmd": str(row.get("ord_tmd") or ""), "src": src}

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


def reconcile_unknowns(nccs: dict | None, ccnl: dict | None,
                       window_s: int = 120) -> list[dict]:
    """원장의 모든 미해소 UNKNOWN을 nccs+ccnl로 대사(신뢰도 판정 포함).

    반환: [{key, symbol, confidence, state, filled, residual, candidates}].
    HIGH만 자동 확정되고(잠금 해제), LOW는 잠금 유지 — 호출부는 LOW를 P0로 알릴 것.
    """
    rows = normalize_rows(nccs, ccnl)
    fold_open = ledger.open_orders()
    # 이미 결속된 ODNO들 — 다른 UNKNOWN의 후보에서 제외(교차 오귀속 방지)
    known = {str(o.get("odno")) for o in fold_open if o.get("odno")}
    results = []
    for o in fold_open:
        if o.get("state") != "unknown" or o.get("reconciled"):
            continue
        key = o["key"]
        cands = candidates_for(o, rows, known_odnos=known - {str(o.get("odno") or "")},
                               window_s=window_s)
        r = ledger.reconcile_from_candidates(key, cands,
                                             intended=o.get("intended"))
        # HIGH로 확정됐고 후보에 ODNO가 있으면 늦게라도 결속(이후 취소/정정 핸들)
        if r.get("confidence") == ledger.CONF_HIGH and cands and cands[0]["odno"]:
            ledger.bind_broker_order(key, cands[0]["odno"],
                                     ord_tmd=cands[0].get("ord_tmd", ""))
        results.append({"key": key, "symbol": o.get("symbol"),
                        "candidates": len(cands), **r})
    return results
