"""슬리브 B(매물대 반등) 성과 통계 — 종목별 수익률(브로커-진실 기준).

A(전환확정)와 분리해 B만 집계한다. kis_positions의 sleeve 태그로 B 종목을
가리고, KIS 잔고(KR+미 3거래소 병합)에서 평단·현재가·평가손익을 읽는다.
조회 실패 시 그 시장은 건너뛴다(부분 집계 표시).

실행: python -m bot.sleeve_stats   (서버에서 — KIS 키 필요)
"""
from __future__ import annotations

from bot import kis, kis_positions

_US_EXCGS = ("NASD", "NYSE", "AMEX")


def _b_tagged() -> dict:
    """{code: 기록} — 슬리브 B로 열린(미청산) 포지션."""
    try:
        recs = kis_positions.load()
    except Exception:
        return {}
    return {c: info for c, info in recs.items() if info.get("sleeve") == "B"}


def _broker_rows() -> tuple[list, bool]:
    """KR+US 병합 보유행 · 전부 성공 여부."""
    rows, ok = [], True
    seen = set()
    kr = kis.positions_detail("KR")
    if kr is None:
        ok = False
    else:
        for p in kr:
            if p["code"] not in seen:
                seen.add(p["code"]); rows.append(p)
    for ex in _US_EXCGS:
        us = kis.positions_detail("US", excg=ex)
        if us is None:
            ok = False; continue
        for p in us:
            if p["code"] not in seen:
                seen.add(p["code"]); rows.append(p)
    return rows, ok


def report_text() -> str:
    b = _b_tagged()
    if not b:
        return "📊 <b>슬리브 B(매물대)</b> — 보유 종목 없음."
    rows, ok = _broker_rows()
    brows = [p for p in rows if p["code"] in b]
    if not brows:
        return ("📊 <b>슬리브 B(매물대)</b> — 태그 %d종목이나 브로커 잔고에 없음"
                "(청산됐거나 조회 실패)." % len(b))
    lines = ["📊 <b>슬리브 B(매물대 반등)</b> — 종목별 수익률", ""]
    tot = {}
    for p in sorted(brows, key=lambda x: -float(x.get("pl_rt") or 0)):
        ccy = p.get("ccy", "USD")
        u = "원" if ccy == "KRW" else "$"
        sign = "🟢" if float(p.get("pl_amt") or 0) >= 0 else "🔴"
        lines.append(f"{sign} {p['name']}({p['code']})  "
                     f"{float(p.get('pl_rt') or 0):+.1f}%  "
                     f"{float(p.get('pl_amt') or 0):+,.0f}{u}")
        t = tot.setdefault(ccy, [0.0, 0.0])
        t[0] += float(p.get("buy_amt") or 0)
        t[1] += float(p.get("pl_amt") or 0)
    lines.append("")
    for ccy, (bu, pl) in tot.items():
        u = "원" if ccy == "KRW" else "$"
        rt = (pl / bu * 100) if bu else 0.0
        sign = "🟢" if pl >= 0 else "🔴"
        lines.append(f"합계 매입 {bu:,.0f}{u} · 손익 {sign} {pl:+,.0f}{u} ({rt:+.1f}%)")
    lines.append(f"\n보유 {len(brows)}종목" + ("" if ok else " · ⚠️ 일부 시장 조회 실패"))
    return "\n".join(lines)


if __name__ == "__main__":
    import re
    print(re.sub(r"</?b>", "", report_text()))
