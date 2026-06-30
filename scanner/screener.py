"""대량 종목 스크리너 — 가벼운 정렬/필터 표(index) + 종목별 상세 차트 페이지.

전 종목 차트를 한 페이지에 그리면 무거우므로 분리한다:
  - index.html          : 점수·RS·신고가·판정 표(한 종목=한 줄) → 수백~수천 종목도 가벼움
  - stocks/{code}.html  : 클릭 시 열리는 상세(일/주/월 차트·지표 토글·신호 카드)
"""
from __future__ import annotations

import html
import os

from scanner import card, earnings, intraday, lwc, names_ko
from scanner.dashboard import _BUCKETS, _bucket

_BUCKET_KO = {"transition": "🟢전환후보", "uptrend": "📈상승추세",
              "watch": "⚪관망", "avoid": "🔴회피"}

# 전환단계 칸 마우스오버 설명(뜻 + 판정 기준)
_STAGE_TIP = {
    4: ("④ 전환 확정 — 하락추세선을 거래량 동반 돌파 → 되눌림 후 안착 → "
        "위로 상승추세선까지 형성. 셋 다 충족된 가장 강한 하락→상승 전환 신호."),
    3: ("③ 돌파후 횡보(대기) — 하락추세선은 넘었고 그 위에서 다지는 중. "
        "상승추세선/거래량이 확인되면 ④ 전환 확정으로 올라감."),
    2: ("② 갓 돌파(미확인) — 하락추세선을 막 넘었지만 되밀릴 수 있어 안착 미확인. 관망."),
    1: ("① 임박 — 아직 하락추세선 아래지만 저항에 바짝 근접. 돌파하면 전환 시작."),
    0: "전환 신호 없음(추세 전환 단계에 해당하지 않음).",
}
# 신호 칸 마우스오버 설명(종합 점수 −100~+100 기준)
_GAUGE_TIP = {
    "🟢 강세": "종합점수 +50 이상 — 적극 매수 구간(여러 지표 강세 정렬).",
    "🟢 관심": "종합점수 +20~+50 — 매수 관심(지지 확인 후 진입).",
    "⚪ 중립": "종합점수 −20~+20 — 관망(방향성 신호 부족).",
    "🔴 주의": "종합점수 −50~−20 — 매도 관심/비중 축소.",
    "🔴 공포": "종합점수 −50 이하 — 적극 회피/청산.",
    "🔴 하락추세": "하락추세선 아래 — 추세 전환 전까지 회피.",
}


def _detail(result: dict, frames: dict) -> str:
    """종목별 상세 페이지 — lightweight-charts(모바일 핀치/팬 부드러움)."""
    return lwc.detail(result, frames)


from scanner.plan import (rec_n as _rec_n, REC_MIN, timing as _timing,
                          thesis as _plan_thesis, junk as _plan_junk)


def _sign(v) -> str:
    """양수=pos(초록)·음수=neg(빨강) 클래스."""
    try:
        return "pos" if float(v) >= 0 else "neg"
    except (TypeError, ValueError):
        return ""


def _mkt_cls(mk: str) -> str:
    if "상승" in mk:
        return "pos"
    if "하락" in mk:
        return "neg"
    return ""


def _rows(results: list[dict]) -> str:
    out = []
    for r in results:
        b = _bucket(r)
        code = r["code"]
        rec = _rec_n(r)
        recd = rec >= REC_MIN
        star = (f'<span class="star" title="진입 추천 — 체크리스트 {rec}/6 충족">'
                f'⭐{rec}</span>') if recd else ""
        rs = r.get("rs", {}).get("rel")
        rs_txt = f"{rs*100:+.0f}%" if rs is not None else "-"
        nh = r.get("newhigh", {}).get("pct_from_high")
        nh_txt = f"{nh:+.0f}%" if nh is not None else "-"
        mk = r.get("market", {}).get("direction", "-")
        tone = html.escape(r.get("trend_oneline", ""))
        stg = r.get("transition_stage", 0)
        stg_lab = html.escape(r.get("transition_label", ""))
        vd = html.escape(r.get("verdict", ""))
        tm = _timing(r)
        if tm:
            vd = f'<b>{html.escape(tm)}</b><br>' + vd
        elif r.get("chase"):
            vd = "🔺추격주의 · " + vd
        edays = earnings.days_until(code)        # 네트워크 0(캐시만)
        if edays is not None and 0 <= edays <= earnings.NEAR_DAYS:
            vd = f"📅어닝 D-{edays}(갭주의) · " + vd
        gauge = r["gauge"]
        gtip = html.escape(_GAUGE_TIP.get(gauge, ""), quote=True)
        stip = html.escape(_STAGE_TIP.get(stg, ""), quote=True)
        ko = names_ko.ko(code)
        ko_html = (f'<span class="ko">{html.escape(ko)}</span>' if ko else "")
        price = (r.get("sr") or {}).get("price")
        price_attr = f"{price:.4f}" if price is not None else ""
        ccy = r.get("ccy", "USD")
        region = "kr" if ccy == "KRW" else "us"
        flag = "🇰🇷" if region == "kr" else "🇺🇸"
        out.append(
            f'<tr class="b-{b}{" rec" if recd else ""}" data-bucket="{b}" '
            f'data-stage="{stg}" data-rec="{rec}" data-code="{code}" '
            f'data-price="{price_attr}" data-ccy="{ccy}" data-region="{region}">'
            f'<td data-label="신호" title="{gtip}">'
            f'<span class="sig">{star}{html.escape(gauge)}</span></td>'
            f'<td class="nm"><button class="hold" onclick="toggleHold(event,\'{code}\')" '
            f'title="내 종목(매수) 담기">☆</button>'
            f'<span class="rgn" title="{"국내(한국)" if region=="kr" else "해외(미국)"}">{flag}</span>'
            f'<a href="stocks/{code}.html">{html.escape(r["name"])}</a>{ko_html}'
            f'<span class="cd">{html.escape(code)}</span>'
            f'<span class="pl" data-code="{code}"></span></td>'
            f'<td data-label="전환단계" data-v="{stg}" class="num stg" '
            f'title="{stip}">{stg_lab}</td>'
            f'<td data-label="추세">{tone}</td>'
            f'<td data-label="점수" data-v="{r["norm"]:.1f}" '
            f'class="num sc {_sign(r["norm"])}">{r["norm"]:+.0f}</td>'
            f'<td data-label="시장"><span class="{_mkt_cls(mk)}">{html.escape(mk)}</span></td>'
            f'<td data-label="RS" data-v="{rs if rs is not None else -999}" '
            f'class="num {_sign(rs) if rs is not None else ""}">{rs_txt}</td>'
            f'<td data-label="신고가" data-v="{nh if nh is not None else -999}" class="num">{nh_txt}</td>'
            f'<td data-label="판정" class="vd">{vd}</td>'
            f'<td class="brk"></td></tr>')
    return "".join(out)


def _rec_card(p: dict, kind: str) -> str:
    """홈 추천 카드 한 장(모의투자 큐레이션과 동일 — 진입 논리 포함)."""
    ccy = p["ccy"]
    fp = (lambda v: f"{v:,.0f}원") if ccy == "KRW" else (lambda v: f"${v:,.2f}")
    vc = "up" if "타당" in p["verdict"] else ("dn" if "회피" in p["verdict"] else "")
    stage = f'<span class="rtag">{html.escape(p["stage"])}</span>' if p["stage"] else ""
    return (
        f'<div class="rc {kind}">'
        f'<div class="rch"><a href="stocks/{p["code"]}.html">{html.escape(p["name"])}</a>'
        f'<span class="rtag">{html.escape(p["sig"])}</span>{stage}'
        f'<span class="rvd {vc}">{html.escape(p["verdict"])}</span></div>'
        f'<div class="rth">🤖 클로드라면: {p["thesis"]}</div>'
        f'<div class="rlv">진입 <b>{fp(p["entry"])}</b> · 손절 {fp(p["stop"])} · '
        f'목표 {fp(p["target"])}</div></div>')


def _recommend_html(results: list[dict]) -> str:
    """홈 상단 '클로드라면 살 종목' 추천 — 모의투자와 같은 큐레이션을 표 위에 노출."""
    picks = _paper_picks(results)
    now = "".join(_rec_card(p, "now") for p in picks["now"])
    watch = "".join(_rec_card(p, "watch") for p in picks["watch"])
    now_sec = (f'<div class="rsec">🟢 지금 진입 검토 <b>{len(picks["now"])}</b></div>{now}'
               if now else
               '<div class="rsec">🟢 지금 진입 검토 <b>0</b></div>'
               '<div class="rmuted">지금 바로 살 자리는 없음(시장 하락 등) — '
               '아래 곧 올 자리 위주로 주시.</div>')
    watch_sec = (f'<div class="rsec" style="margin-top:12px">👀 곧 올 자리·전환 임박 '
                 f'<b>{len(picks["watch"])}</b></div>{watch}' if watch else "")
    return (f'<details class="reco" open>'
            f'<summary>🤖 클로드라면 살 종목 — AI 큐레이션 추천 (진입 논리 포함)</summary>'
            f'<div class="rbody">{now_sec}{watch_sec}'
            f'<div class="rmuted">⚠️ 차트 기준 추천 · 투자권유 아님. 종목 눌러 상세 확인 · '
            f'<a href="paper.html" style="color:#15803d;font-weight:700">💰 모의투자로 연습</a></div>'
            f'</div></details>')


def _index(results: list[dict]) -> str:
    import datetime
    from scanner import cache, universe
    # 기본 정렬: 진입 추천 점수 → 전환 단계 → 종합점수 (추천 종목이 맨 위로)
    results = sorted(results,
                     key=lambda r: (_rec_n(r), r.get("transition_stage", 0), r["norm"]),
                     reverse=True)
    counts = {k: sum(1 for r in results if _bucket(r) == k) for k, _ in _BUCKETS}
    rcount = sum(1 for r in results if _rec_n(r) >= REC_MIN)
    chips = "".join(
        f'<button class="chip" onclick="flt(\'{k}\')">{_BUCKET_KO[k]} {counts[k]}</button>'
        for k, _ in _BUCKETS)
    # 전환단계 ①~④ 각각 필터
    stage_lab = {1: "①임박", 2: "②갓돌파", 3: "③횡보", 4: "④확정"}
    scnt = {s: sum(1 for r in results if r.get("transition_stage", 0) == s)
            for s in (1, 2, 3, 4)}
    stage_chips = "".join(
        f'<button class="chip stagechip" onclick="fltStageN({s})">{stage_lab[s]} {scnt[s]}</button>'
        for s in (1, 2, 3, 4) if scnt[s])
    # 국내(한국)/해외(미국) 구분 필터
    n_us = sum(1 for r in results if r.get("ccy") != "KRW")
    n_kr = sum(1 for r in results if r.get("ccy") == "KRW")
    region_chips = (
        f'<button class="chip rgnchip" onclick="fltRegion(\'us\')">🇺🇸 해외 {n_us}</button>'
        f'<button class="chip rgnchip" onclick="fltRegion(\'kr\')">🇰🇷 국내 {n_kr}</button>')
    # 수집 진행률(캐시된 종목 / 유니버스 전체)
    try:
        cached = len(cache.cached_codes())
        uni = len([s for s in universe.load() if s.get("code")]) or 1
    except Exception:
        cached, uni = len(results), max(len(results), 1)
    pct = min(100, round(cached / uni * 100))
    updated = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return _INDEX_TMPL.format(
        n=len(results), rows=_rows(results), chips=chips, rcount=rcount,
        recmin=REC_MIN, stage_chips=stage_chips, region_chips=region_chips,
        reco=_recommend_html(results),
        cached=cached, uni=uni, pct=pct, updated=updated)


def build(results: list[dict], frames_map: dict[str, dict],
          out_dir: str = "public", metas: dict | None = None) -> str:
    """스크리너(index.html) + 종목별 상세(stocks/*.html) 생성. out_dir 반환."""
    import config
    os.makedirs(os.path.join(out_dir, "stocks"), exist_ok=True)
    h_min = config.MA_PERIODS["H"][-1] + 5
    for r in results:
        code = r["code"]
        frames = frames_map[code]
        h = intraday.frame(code)                 # 시간봉 캐시(네트워크 0). 없으면 None
        if h is not None and len(h) >= h_min:
            frames = {**frames, "H": h}          # 충분하면 '시간봉' 탭 추가
        path = os.path.join(out_dir, "stocks", f"{code}.html")
        try:
            page = _detail(r, frames)
        except Exception:                        # 한 종목 실패가 전체 빌드를 막지 않도록
            page = _detail(r, {k: v for k, v in frames.items() if k != "H"})
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(page)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fp:
        fp.write(_index(results))
    with open(os.path.join(out_dir, "lookup.html"), "w", encoding="utf-8") as fp:
        fp.write(_REPO and _trigger_page())   # 웹 즉석 조회(워크플로 트리거) 페이지
    with open(os.path.join(out_dir, "guide.html"), "w", encoding="utf-8") as fp:
        fp.write(_GUIDE_HTML)                  # 매매 가이드(읽기 전용 안내)
    with open(os.path.join(out_dir, "paper.html"), "w", encoding="utf-8") as fp:
        fp.write(_paper_page(results))         # 모의투자(페이퍼 트레이딩)
    return out_dir


# '이미 폭등' 추천 제외 기준 — config 단일 출처(조절은 config.BLOWOFF_RATIO).
import config as _cfg
BLOWOFF = _cfg.BLOWOFF_RATIO


# ── 모의투자(페이퍼 트레이딩) 페이지 ──────────────────────────────
def _pick_item(r: dict, th: dict) -> dict:
    risk = r.get("risk") or {}
    p = (r.get("sr") or {}).get("price")
    return {
        "code": r["code"], "name": r["name"], "ccy": r.get("ccy", "USD"),
        "price": round(float(p), 4) if p else 0,
        "sig": r["gauge"], "stage": r.get("transition_label") or "",
        "verdict": th["verdict"], "thesis": th["thesis"],
        "entry": round(float(r.get("entry") or 0), 4),
        "stop": round(float((risk.get("stop") or 0)), 4),
        "target": round(float((risk.get("target") or 0)), 4),
    }


def _paper_picks(results: list[dict]) -> dict:
    """추천 큐레이션: '지금 진입 검토'(now) vs '곧 올 자리·전환 임박'(watch).

    품질 게이트(사용자 검토 반영): ① 저유동성(거래 불가 잡주) 제외, ② 장기 폭등(이미
    많이 오른 추격)은 '지금 진입'에서 제외, ③ 손절폭 과대(R:R 나쁨)는 '지금 진입' 제외.
    """
    import config
    now, watch = [], []
    for r in results:
        if r.get("vetoed") or r.get("entry_kind") == "avoid":
            continue
        if _plan_junk(r):              # 동전주($1·1000원 미만)·심한 부실(−85%↓) 제외
            continue
        # ① 유동성: 20일 평균 거래대금 낮으면(잡주, 거래 불가) 추천 자체에서 제외
        ccy = r.get("ccy", "USD")
        turn = r.get("turnover", 0) or 0
        liq_min = config.LIQ_MIN_USD if ccy != "KRW" else config.LIQ_MIN_KRW
        if turn < liq_min:
            continue
        # ② '이미 폭등' 하드 제외(now·watch 모두): 추천 안 함.
        #   기준 = 지수 대비 3개월 상대강도(RS) +BLOWOFF↑  또는  120일선 대비 +BLOWOFF↑
        ext = r.get("ext") or {}
        rs_rel = (r.get("rs") or {}).get("rel") or 0
        if rs_rel >= BLOWOFF or ext.get("ma120_stretch", 0) >= BLOWOFF:
            continue
        th = _plan_thesis(r)
        rec = _rec_n(r)
        tm = _timing(r)
        stage = r.get("transition_stage", 0)
        kind = r.get("entry_kind", "now")
        item = _pick_item(r, th)
        # ③ 손절폭 과대 = R:R 나쁨. 지금진입은 12%↑ 제외, 관찰은 15%↑ 제외.
        entry = r.get("entry") or 0
        stop = (r.get("risk") or {}).get("stop") or 0
        price = (r.get("sr") or {}).get("price") or 0
        stop_pct = (entry - stop) / entry if entry else 0
        # ④ 과도한 눌림: 눌림 목표가 현재가보다 25%+ 아래 = '곧 올 자리' 아님(대폭락 대기)
        far_pull = (kind == "pullback" and price
                    and (price - entry) / price >= 0.25)
        # '지금 진입'(조건부 포함)은 깨끗한 셋업만 — 정배열(상승 정렬) 또는 전환 ③④.
        #   혼조·횡보·단기 하락 중(예: NXPI)인데 점수만 높은 건 제외.
        arr = (r.get("trend") or {}).get("arrangement")
        clean = (arr == "정배열") or stage >= 3
        # 깨끗한 셋업(정배열/전환③④)이 아니면 '지금 진입' 안 함 — 🎯 타점권 경로도 포함.
        #   (rec 6/6은 이미 전환단계 포함이라 통과) 혼조·하락 중(NXPI·COCO)은 전부 제외.
        is_now = th["now"] and (stop_pct < 0.12) and (
            rec >= REC_MIN
            or (clean and ((tm and "🎯" in tm)
                           or (kind == "now" and r.get("norm", 0) >= config.VERDICT_WEAK))))
        is_watch = ((not th["now"]) and (stop_pct < 0.15) and (not far_pull)
                    and (kind in ("pullback", "breakout") or stage in (1, 2)))
        if is_now:
            now.append((rec, r.get("norm", 0), item))
        elif is_watch:
            watch.append((stage, r.get("norm", 0), item))
    now.sort(key=lambda x: (x[0], x[1]), reverse=True)
    watch.sort(key=lambda x: (x[0], x[1]), reverse=True)

    def _dedup(rows, seen, n):
        out = []
        for _a, _b, i in rows:
            if i["name"] in seen:
                continue
            seen.add(i["name"])
            out.append(i)
            if len(out) >= n:
                break
        return out

    seen = set()
    now_p = _dedup(now, seen, 16)
    watch_p = _dedup(watch, seen, 16)
    return {"now": now_p, "watch": watch_p}


def _paper_page(results: list[dict]) -> str:
    import json
    prices = {}
    for r in results:
        p = (r.get("sr") or {}).get("price")
        if p is None:
            continue
        prices[r["code"]] = [r["name"], round(float(p), 4), r.get("ccy", "USD")]
    picks = _paper_picks(results)
    return (_PAPER_TMPL
            .replace("__PRICES__", json.dumps(prices, ensure_ascii=False))
            .replace("__PICKS__", json.dumps(picks, ensure_ascii=False))
            .replace("__FX__", "1380"))


# 저장소 정보(워크플로 트리거 대상). 다른 저장소면 여기만 바꾸면 됨.
_REPO = "easyseop/Stock-chart-analyze"


def _trigger_page() -> str:
    return _LOOKUP_TMPL.replace("__REPO__", _REPO)


_INDEX_TMPL = """<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>종목 스크리너</title>
<style>
  body{{margin:0;font-family:-apple-system,'Segoe UI','Noto Sans KR',sans-serif;
    background:#f1f5f9;color:#1e293b}}
  header{{background:#0f172a;color:#fff;padding:14px 20px}}
  header h1{{margin:0;font-size:17px}}
  header p{{margin:4px 0 0;font-size:12px;color:#94a3b8}}
  .bar{{padding:10px 16px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
  .chip{{border:1px solid #cbd5e1;background:#fff;border-radius:999px;padding:6px 14px;
    cursor:pointer;font-size:13px}}
  .chip.on{{background:#2563eb;border-color:#2563eb;color:#fff}}
  table{{width:100%;border-collapse:collapse;background:#fff;font-size:13px}}
  th,td{{padding:8px 10px;border-bottom:1px solid #eef2f7;text-align:right}}
  th{{background:#f8fafc;color:#64748b;font-size:12px;position:sticky;top:0;cursor:pointer;
    user-select:none}}
  th:nth-child(2),td.nm{{text-align:left}}
  td.vd{{text-align:left;color:#475569;max-width:360px}}
  td.sc{{font-weight:700}}
  .num{{font-variant-numeric:tabular-nums}}
  td.stg{{text-align:left;color:#16a34a;font-weight:600;font-size:12px}}
  .nm a{{color:#1d4ed8;text-decoration:none;font-weight:600}}
  .nm .ko{{color:#475569;font-size:12px;margin-left:6px}}
  .nm .cd{{color:#94a3b8;font-size:11px;margin-left:6px}}
  .legend{{margin:0 16px 4px;background:#fff;border:1px solid #e2e8f0;border-radius:10px}}
  .legend summary{{cursor:pointer;padding:9px 13px;font-size:13px;font-weight:600;color:#334155}}
  .legend .lc{{padding:4px 15px 13px;font-size:12.5px;color:#475569;line-height:1.75}}
  .legend b{{color:#0f172a}}
  td.sc.pos,.num.pos,span.pos{{color:#16a34a}}   /* 양수=초록 */
  td.sc.neg,.num.neg,span.neg{{color:#dc2626}}   /* 음수=빨강 */
  tr.rec{{background:#fffbeb}}
  .sig{{display:inline-flex;align-items:center;gap:5px}}
  .star{{font-size:11px;font-weight:700;color:#d97706}}
  .rec-chip{{background:#fef3c7;border-color:#f59e0b;color:#92400e;font-weight:700}}
  .rec-chip.on{{background:#f59e0b;border-color:#f59e0b;color:#fff}}
  .bar2{{padding-top:0}} .barlbl{{font-size:12px;color:#94a3b8;font-weight:600;align-self:center}}
  .stagechip{{background:#ecfdf5;border-color:#a7f3d0;color:#065f46;font-size:12px}}
  .stagechip.on{{background:#16a34a;border-color:#16a34a;color:#fff}}
  .holdchip{{background:#fffbeb;border-color:#f59e0b;color:#92400e;font-weight:700}}
  .holdchip.on{{background:#f59e0b;border-color:#f59e0b;color:#fff}}
  .rgnchip{{background:#f8fafc;font-weight:700}}
  .rgnchip.on{{background:#334155;border-color:#334155;color:#fff}}
  .rgn{{font-size:12px;margin-right:4px;vertical-align:middle}}
  .hold{{border:0;background:none;cursor:pointer;font-size:16px;color:#cbd5e1;padding:0 6px 0 0;
    line-height:1;vertical-align:middle}}
  .hold.on{{color:#f59e0b}}
  .pl{{font-size:11.5px;font-weight:700;margin-left:7px;white-space:nowrap}}
  .pl.up{{color:#16a34a}} .pl.dn{{color:#dc2626}}
  tr.held{{background:#fffdf3}}
  .pos{{color:#16a34a}}.neg{{color:#dc2626}}
  .notice{{margin:8px 16px 0;background:#fff;border:1px solid #e2e8f0;border-radius:10px}}
  .notice>summary{{cursor:pointer;padding:9px 13px;font-size:13px;font-weight:600;color:#334155}}
  .notice .nc{{padding:2px 15px 12px;font-size:12.5px;color:#475569;line-height:1.8}}
  .notice .nc b{{color:#0f172a}} .notice .ndim{{color:#94a3b8;font-size:11.5px}}
  .reco{{margin:10px 16px 0;background:#fff;border:1px solid #e2e8f0;border-radius:12px}}
  .reco>summary{{cursor:pointer;padding:11px 14px;font-size:14px;font-weight:800;color:#0f172a}}
  .rbody{{padding:4px 14px 12px}}
  .rsec{{font-size:13px;font-weight:800;color:#334155;margin:8px 0 7px}}
  .rsec b{{color:#2563eb}}
  .rc{{border:1px solid #e2e8f0;border-radius:10px;padding:10px 11px;margin-bottom:8px}}
  .rc.now{{border-left:4px solid #16a34a}} .rc.watch{{border-left:4px solid #f59e0b}}
  .rch{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
  .rch a{{font-size:14.5px;font-weight:700;color:#0f172a;text-decoration:none}}
  .rtag{{font-size:11px;font-weight:700;padding:2px 7px;border-radius:999px;background:#f1f5f9;color:#475569}}
  .rvd{{font-size:12px;font-weight:800;margin-left:auto}}
  .rvd.up{{color:#16a34a}} .rvd.dn{{color:#dc2626}}
  .rth{{font-size:12.5px;color:#334155;line-height:1.6;margin:6px 0 5px}}
  .rth b{{color:#0f172a}}
  .rlv{{font-size:12px;color:#475569}} .rlv b{{color:#0f172a}}
  .rmuted{{font-size:12px;color:#94a3b8;padding:4px 0}}
  .prog{{padding:8px 16px 0}}
  .ptxt{{font-size:12px;color:#475569;margin-bottom:4px}}
  .pbarw{{height:10px;background:#e2e8f0;border-radius:999px;overflow:hidden}}
  .pfillw{{height:100%;background:linear-gradient(90deg,#16a34a,#22c55e);border-radius:999px}}
  .search{{position:sticky;top:0;z-index:9;padding:8px 16px;display:flex;
    align-items:center;gap:8px;background:#f1f5f9;border-bottom:1px solid #e2e8f0}}
  .search input{{flex:1;box-sizing:border-box;padding:12px 14px;border:1px solid #cbd5e1;
    border-radius:10px;font-size:16px;background:#fff}}
  .search input:focus{{outline:none;border-color:#2563eb}}
  .qn{{font-size:12px;color:#64748b;white-space:nowrap}}
  .tw{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
  td.brk{{display:none}}   /* 모바일 카드에서만 줄바꿈용으로 사용 */
  /* 모바일: 증권사 앱 스타일 카드 (한 종목 = 카드 한 장) */
  @media(max-width:640px){{
    header h1{{font-size:15px}} header p{{display:none}}
    .chip{{padding:6px 11px;font-size:12px}}
    .tw{{overflow:visible}}
    thead{{display:none}}
    table,tbody{{display:block}}
    tr{{display:flex;flex-wrap:wrap;align-items:center;gap:7px 7px;position:relative;
       background:#fff;border:1px solid #e2e8f0;border-radius:14px;
       margin:10px 12px;padding:13px 14px;box-shadow:0 1px 3px rgba(15,23,42,.05)}}
    tr.b-transition{{border-left:4px solid #16a34a}}
    tr.b-uptrend{{border-left:4px solid #38bdf8}}
    tr.b-avoid{{border-left:4px solid #dc2626}}
    tr.rec{{background:#fffdf5;border-color:#f1c453}}
    td{{display:block;border:0;padding:0;font-size:13px;text-align:left}}
    td::before{{content:attr(data-label);display:block;color:#94a3b8;font-size:10px;
       font-weight:700;margin-bottom:1px}}
    /* 종목명: 맨 위 전체폭, 크게 */
    td.nm{{order:1;flex-basis:100%}}
    td.nm::before{{display:none}}
    td.nm a{{font-size:17px;font-weight:700;line-height:1.25}}
    td.nm .ko{{display:inline;margin-left:6px;font-size:12px;color:#64748b}}
    td.nm .cd{{display:block;margin:2px 0 0;font-size:11px}}
    /* 신호·전환단계·추세: 상태 태그(흐름대로 칩) */
    td[data-label="신호"],td.stg,td[data-label="추세"]{{order:2;flex:0 0 auto;
       background:#f1f5f9;border-radius:999px;padding:5px 11px;font-size:12px;font-weight:700}}
    td[data-label="신호"]::before,td.stg::before,td[data-label="추세"]::before{{display:none}}
    td.stg{{color:#16a34a}}
    td[data-v="0"].stg,td[data-v=""].stg{{display:none}}
    /* 메트릭 줄바꿈 강제(태그와 분리) */
    td.brk{{display:block;order:4;flex-basis:100%;height:0;padding:0;margin:0}}
    td.brk::before{{display:none}}
    /* 점수·시장·RS·신고가: 인라인 메트릭 한 줄 'label 값' */
    td[data-label="점수"],td[data-label="시장"],td[data-label="RS"],td[data-label="신고가"]{{
       order:5;flex:0 0 auto;font-size:13.5px;font-weight:700;color:#0f172a;
       background:#f8fafc;border-radius:8px;padding:4px 9px;white-space:nowrap}}
    td[data-label="점수"]::before,td[data-label="시장"]::before,
    td[data-label="RS"]::before,td[data-label="신고가"]::before{{
       display:inline;font-size:11px;color:#94a3b8;font-weight:600;margin:0 4px 0 0}}
    /* 판정: 맨 아래 전체폭, 옅게 */
    td.vd{{order:9;flex-basis:100%;color:#94a3b8;font-size:11.5px;line-height:1.45;
       border-top:1px solid #f1f5f9;padding-top:7px;margin-top:2px}}
    td.vd::before{{display:none}}
    td.vd b{{color:#475569}}
  }}
</style></head><body>
<header><h1>종목 스크리너 <span style="color:#38bdf8;font-size:13px">차트 신호 랭킹</span></h1>
<p>{n}종목 표시 · 헤더 클릭=정렬 · 칩=필터 · 종목명 클릭=상세 차트(일/주/월)</p></header>
<div class="prog">
  <div class="ptxt">📥 수집 진행 <b>{cached}/{uni}</b> ({pct}%) · 마지막 갱신 {updated}</div>
  <div class="pbarw"><div class="pfillw" style="width:{pct}%"></div></div>
</div>
<details class="notice"><summary>🕐 차트 데이터는 언제 갱신되나요?</summary>
<div class="nc">
<b>🇺🇸 미국주</b> — 미 장중 <b>15분마다</b>(대략 밤 22:30~새벽 05:00 KST) 가격 갱신.<br>
<b>🇰🇷 한국주</b> — 한국 장중 <b>15분마다</b>(09:00~15:30 KST) 가격 갱신.<br>
<b>📅 매일 마감 후</b> — 전체 종목 갱신 + 새 종목 백필(미수집분 채움).<br>
<b>💼 내 종목 · ➕ 즉석조회 종목</b>도 위 장중 갱신에 함께 포함돼요(한국주는 한국 장중, 미국주는 미 장중).<br>
<span class="ndim">※ 시간은 GitHub Actions 스케줄 기준이라 ±5~10분 차이날 수 있어요. 페이지 상단 "마지막 갱신"이 실제 반영 시각.</span>
</div></details>
<div class="search">
  <input id="q" type="search" inputmode="search" autocomplete="off"
    placeholder="🔍 종목 검색 (티커·영문명·한글명)" oninput="search(this.value)">
  <span id="qn" class="qn"></span>
</div>
<details class="legend"><summary>❔ 전환후보 / 전환단계 / 신호 / 시장 — 뜻과 구분 (꼭 읽기)</summary>
<div class="lc">
세 가지는 <b>서로 다른 축</b>이에요. 헷갈리지 않게 구분하면:<br><br>
<b>① 상태 그룹</b> (위쪽 칩 — "이 종목이 추세 사이클 어디?"):<br>
&nbsp;<b>🟢 전환후보</b>=하락→상승 <b>전환 진행 중</b>(①~④단계) · <b>📈 상승추세</b>=전환 끝나고 <b>이미 강세</b> ·
 <b>⚪ 관망</b>=방향 미정 · <b>🔴 회피</b>=하락추세.<br><br>
<b>② 전환단계</b> (①~④ — "전환후보 <b>안에서</b> 얼마나 진행됐나", 클수록 강함):<br>
&nbsp;<b>① 임박</b> — 아직 하락추세선 아래지만 저항 근접 · <b>② 갓 돌파(미확인)</b> — 막 넘었으나 안착 미확인 →
 <b>③ 돌파후 횡보</b> — 위에서 다지는 중 → <b>④ 전환 확정 ⭐</b> — 돌파+안착+상승추세선+거래량 다 충족(가장 강함).<br>
&nbsp;<span style="color:#94a3b8">※ ①②③④는 "🟢전환후보" 그룹의 세부 단계예요. 상승추세/관망/회피엔 전환단계가 없음(–).</span><br><br>
<b>③ 신호</b> (종합점수 −100~+100, <b>또 다른 축</b>): <b>🟢강세</b> +50↑ · <b>🟢관심</b> +20~50 ·
 <b>⚪중립</b> ±20 · <b>🔴주의</b> −50~−20 · <b>🔴공포</b> −50↓.<br>
&nbsp;<span style="color:#94a3b8">※ "신호 🟢강세"인데 "전환후보"가 아닐 수 있어요 — 점수는 강해도 전환 셋업은 아닌 경우. 칩=추세 상태, 신호=점수.</span><br><br>
<b>④ 시장</b> (지수 방향): <span style="color:#16a34a;font-weight:700">상승</span> /
 <span style="color:#dc2626;font-weight:700">하락</span>. <b>가장 중요한 필터예요.</b><br>
&nbsp;<b>❗ 전환후보인데 시장이 하락이면?</b> → 종목은 바닥에서 도는 중이지만 <b>시장이 안 받쳐주면 전환 실패(되돌림) 위험</b>이 커요.
 그래서 <b>진입추천(⭐)은 시장이 상승일 때만</b> 나와요(지금 시장 하락이면 추천 0이 정상). <b>전환후보+시장하락 = "지금 사지 말고 관찰, 시장 돌면 1순위"</b>.
</div></details>
<div class="bar">
  <button class="chip rec-chip on" onclick="fltRec()">⭐ 진입 추천 {rcount}</button>
  <button class="chip" onclick="flt('all')">전체</button>
  {region_chips}
  {chips}
</div>
<div class="bar bar2"><span class="barlbl">전환단계</span>{stage_chips}
  <button class="chip holdchip" id="holdchip" onclick="fltHold()" style="display:none">💼 내 종목 <span id="holdn">0</span></button>
  <a class="chip" href="paper.html" style="background:#15803d;color:#fff;border-color:#15803d;text-decoration:none">💰 모의투자</a>
  <a class="chip" href="guide.html" style="text-decoration:none">📘 가이드</a>
  <a class="chip" href="lookup.html" style="background:#0f172a;color:#fff;border-color:#0f172a;text-decoration:none">➕ 즉석조회</a>
</div>
{reco}
<div class="tw"><table id="t"><thead><tr>
  <th onclick="srt(0,false)">신호</th>
  <th onclick="srt(1,false)">종목</th>
  <th onclick="srt(2,true)">전환단계▼</th>
  <th onclick="srt(3,false)">추세</th>
  <th onclick="srt(4,true)">점수</th>
  <th onclick="srt(5,false)">시장</th>
  <th onclick="srt(6,true)">RS</th>
  <th onclick="srt(7,true)">신고가</th>
  <th onclick="srt(8,false)">판정</th>
</tr></thead><tbody id="tb">{rows}</tbody></table></div>
<script>
  var tb=document.getElementById('tb');
  function srt(col,num){{
    var rows=[].slice.call(tb.querySelectorAll('tr'));
    rows.sort(function(a,b){{
      var x=a.children[col],y=b.children[col];
      if(num){{return parseFloat(y.dataset.v)-parseFloat(x.dataset.v);}}
      return (x.textContent>y.textContent?1:-1);
    }});
    rows.forEach(function(r){{tb.appendChild(r);}});
  }}
  function setOn(btn){{
    document.querySelectorAll('.chip').forEach(function(c){{c.classList.remove('on');}});
    if(btn) btn.classList.add('on');
  }}
  function flt(b){{
    setOn(typeof event!=='undefined'?event.target:null);
    tb.querySelectorAll('tr').forEach(function(r){{
      r.style.display=(b==='all'||r.dataset.bucket===b)?'':'none';
    }});
  }}
  function fltStage(){{
    setOn(typeof event!=='undefined'?event.target:null);
    tb.querySelectorAll('tr').forEach(function(r){{
      r.style.display=(parseInt(r.dataset.stage||'0')>0)?'':'none';
    }});
  }}
  // 국내(한국)/해외(미국) 필터
  function fltRegion(rg){{
    setOn(typeof event!=='undefined'?event.target:null);
    tb.querySelectorAll('tr').forEach(function(r){{
      r.style.display=(r.dataset.region===rg)?'':'none';
    }});
  }}
  // 전환단계 ①~④ 각각 필터
  function fltStageN(n){{
    setOn(typeof event!=='undefined'?event.target:null);
    tb.querySelectorAll('tr').forEach(function(r){{
      r.style.display=(parseInt(r.dataset.stage||'0')===n)?'':'none';
    }});
  }}
  // ⭐ 진입 추천: 체크리스트 4/6 이상만 표시(가장 손쉬운 후보 추리기)
  function fltRec(){{
    setOn(typeof event!=='undefined'?event.target:document.querySelector('.rec-chip'));
    tb.querySelectorAll('tr').forEach(function(r){{
      r.style.display=(parseInt(r.dataset.rec||'0')>={recmin})?'':'none';
    }});
  }}
  // 검색: 티커·영문명·한글명. 성능 위해 검색 인덱스를 한 번만 만들고(매 글자마다 DOM
  //   재조회 안 함), 입력은 디바운스. 검색 중엔 추천·안내·필터바를 숨겨 결과를 위로.
  var IDX=null, _stmr=null;
  function buildIdx(){{ IDX=[].slice.call(tb.querySelectorAll('tr')).map(function(r){{
    var nm=r.querySelector('.nm'); return [r,(nm?nm.textContent:'').toLowerCase()]; }}); }}
  function search(v){{ clearTimeout(_stmr); _stmr=setTimeout(function(){{doSearch(v);}},120); }}
  function doSearch(v){{
    if(!IDX) buildIdx();
    v=(v||'').trim().toLowerCase();
    var qn=document.getElementById('qn'), on=!!v;
    // 검색 중 화면 정리: 추천·안내·범례·필터바 숨김 → 결과가 검색창 바로 아래로
    document.querySelectorAll('.reco,.notice,.legend,.bar').forEach(function(e){{e.style.display=on?'none':'';}});
    if(!v){{ setOn(document.querySelectorAll('.chip')[0]);
      for(var i=0;i<IDX.length;i++) IDX[i][0].style.display='';
      qn.textContent=''; return; }}
    setOn(null); var hit=0;
    for(var i=0;i<IDX.length;i++){{ var ok=IDX[i][1].indexOf(v)>=0;
      IDX[i][0].style.display=ok?'':'none'; if(ok) hit++; }}
    qn.textContent=hit+'개';
  }}
  // 💼 내 종목(매수/즐겨찾기) — 브라우저 localStorage에만 저장. 매수가 넣으면 손익(P/L) 표시.
  function loadHold(){{try{{return JSON.parse(localStorage.getItem('holdings')||'{{}}');}}catch(e){{return {{}};}}}}
  function saveHold(h){{localStorage.setItem('holdings',JSON.stringify(h));}}
  function toggleHold(e,code){{
    e.preventDefault();e.stopPropagation();
    var h=loadHold();
    if(h[code]){{ if(confirm(code+' 을(를) 내 종목에서 뺄까요?')){{delete h[code];}} }}
    else{{
      var row=tb.querySelector('tr[data-code="'+code+'"]');
      var cur=row?parseFloat(row.dataset.price):NaN;
      var v=prompt(code+' 매수가 입력(손익 표시) · 비우면 즐겨찾기만'
        +(isFinite(cur)?'\\n현재가 '+cur:''),'');
      if(v===null)return;                  // 취소
      var buy=v?parseFloat(v.replace(/[^0-9.]/g,'')):0;
      h[code]={{buy:isFinite(buy)?buy:0,ts:Date.now()}};
    }}
    saveHold(h);renderHold();
  }}
  function renderHold(){{
    var h=loadHold(),keys=Object.keys(h);
    document.getElementById('holdn').textContent=keys.length;
    document.getElementById('holdchip').style.display=keys.length?'':'none';
    tb.querySelectorAll('tr').forEach(function(r){{
      var code=r.dataset.code,on=!!h[code];
      r.classList.toggle('held',on);
      var btn=r.querySelector('.hold');
      if(btn){{btn.textContent=on?'★':'☆';btn.classList.toggle('on',on);}}
      var pl=r.querySelector('.pl[data-code="'+code+'"]');
      if(pl){{
        pl.className='pl';pl.innerHTML='';
        if(on&&h[code].buy){{
          var cur=parseFloat(r.dataset.price);
          if(isFinite(cur)){{
            var pct=(cur/h[code].buy-1)*100;
            pl.classList.add(pct>=0?'up':'dn');
            pl.innerHTML=(pct>=0?'+':'')+pct.toFixed(1)+'% '
              +'<span style="color:#94a3b8;font-weight:400">@'+h[code].buy+'</span>';
          }}
        }}
      }}
    }});
  }}
  function fltHold(){{
    setOn(typeof event!=='undefined'?event.target:null);
    var h=loadHold();
    tb.querySelectorAll('tr').forEach(function(r){{
      r.style.display=h[r.dataset.code]?'':'none';
    }});
  }}
  // 첫 화면: ⭐진입 추천(체크리스트 4/6+)만 우선. 없으면 전환후보, 그래도 없으면 전체.
  window.addEventListener('load',function(){{
    renderHold();
    var rec=0,trans=0;
    tb.querySelectorAll('tr').forEach(function(r){{
      if(parseInt(r.dataset.rec||'0')>={recmin})rec++;
      if(r.dataset.bucket==='transition')trans++;}});
    if(rec){{fltRec();}}
    else if(trans){{setOn(document.querySelector('.rec-chip'));
      document.querySelector('.rec-chip').classList.remove('on');
      var tc=Array.prototype.find.call(document.querySelectorAll('.chip'),
        function(c){{return c.textContent.indexOf('전환후보')>=0;}});
      setOn(tc);
      tb.querySelectorAll('tr').forEach(function(r){{
        r.style.display=(r.dataset.bucket==='transition')?'':'none';}});}}
    else{{setOn(document.querySelector('.chip:nth-child(2)'));
      tb.querySelectorAll('tr').forEach(function(r){{r.style.display='';}});}}
  }});
</script></body></html>"""


_LOOKUP_TMPL = """<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>티커 즉석 조회</title>
<style>
  body{margin:0;font-family:-apple-system,'Segoe UI','Noto Sans KR',sans-serif;
    background:#f1f5f9;color:#1e293b}
  header{background:#0f172a;color:#fff;padding:14px 20px}
  header h1{margin:0;font-size:17px} header a{color:#7dd3fc;font-size:13px;text-decoration:none}
  .wrap{max-width:620px;margin:0 auto;padding:18px}
  .card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:14px}
  label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px;color:#334155}
  input{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #cbd5e1;
    border-radius:8px;font-size:15px}
  button.go{width:100%;margin-top:14px;padding:13px;border:0;border-radius:10px;
    background:#2563eb;color:#fff;font-size:16px;font-weight:700;cursor:pointer}
  button.go:disabled{background:#94a3b8}
  .hint{font-size:12px;color:#64748b;line-height:1.6;margin-top:6px}
  .tkchk{font-size:13px;margin-top:7px;min-height:18px;font-weight:600}
  .tkchk.ok{color:#16a34a} .tkchk.bad{color:#dc2626}
  .stat{font-size:14px;margin-top:12px;padding:12px;border-radius:8px;background:#f8fafc;
    border:1px solid #e2e8f0;display:none}
  .stat.on{display:block}
  .pbar{height:12px;background:#e2e8f0;border-radius:999px;overflow:hidden;margin-top:12px;display:none}
  .pbar.on{display:block}
  .pfill{height:100%;width:0;background:#2563eb;transition:width .5s ease;border-radius:999px}
  .pfill.anim{background-image:linear-gradient(90deg,#2563eb,#60a5fa,#2563eb);background-size:200% 100%;animation:flow 1.2s linear infinite}
  @keyframes flow{0%{background-position:0 0}100%{background-position:-200% 0}}
  details summary{cursor:pointer;font-weight:600;color:#334155}
  code{background:#f1f5f9;padding:1px 5px;border-radius:4px}
  .hist-item{display:flex;align-items:center;gap:10px;padding:9px 2px;
    border-bottom:1px solid #f1f5f9;font-size:14px}
  .hist-item:last-child{border-bottom:0}
  .hist-item a.t{font-weight:700;color:#1d4ed8;text-decoration:none}
  .hist-item .s{font-size:12px;color:#64748b;margin-left:auto}
  .hist-item .x{color:#cbd5e1;cursor:pointer;font-size:18px;line-height:1}
</style></head><body>
<header><a href="index.html">&larr; 스크리너</a>
<h1>티커 즉석 조회 <span style="color:#38bdf8;font-size:13px">웹에서 바로 수집</span></h1></header>
<div class="wrap">
  <div class="card">
    <label>티커 / 코드</label>
    <input id="tk" placeholder="예: AAPL, NVDA, 005930" autocapitalize="characters"
      oninput="validate(this.value)">
    <div id="tkchk" class="tkchk"></div>
    <button class="go" id="go" onclick="run()">수집하기 (워크플로 실행)</button>
    <div class="stat" id="st"></div>
    <div class="pbar" id="pb"><div class="pfill" id="pf"></div></div>
    <div class="hint">미국 티커 또는 한국 6자리 코드. 누르면 GitHub Actions의 lookup 워크플로가
      돌아 그 종목을 분석합니다(약 1~2분). 끝나면 결과 링크가 떠요.</div>
  </div>
  <div class="card" id="histcard" style="display:none">
    <div style="font-weight:700;margin-bottom:6px">📋 즉석조회한 종목 <span id="histn" style="color:#94a3b8;font-weight:400;font-size:13px"></span></div>
    <div id="hist"></div>
    <div style="text-align:right;margin-top:6px"><span id="histclear" onclick="clearHist()" style="font-size:12px;color:#94a3b8;cursor:pointer">목록 비우기</span></div>
  </div>
  <div class="card">
    <details><summary>최초 1회: 내 GitHub 토큰 입력 (브라우저에만 저장)</summary>
    <label>GitHub 토큰 (PAT)</label>
    <input id="pat" type="password" placeholder="github_pat_...">
    <div class="hint">웹에서 워크플로를 켜려면 토큰이 필요합니다. <b>이 토큰은 당신 브라우저에만
      저장</b>(localStorage)되고 어디에도 전송/공개되지 않습니다(깃허브 API 호출에만 사용).<br>
      만드는 법: GitHub &rarr; Settings &rarr; Developer settings &rarr;
      <b>Fine-grained tokens</b> &rarr; 이 저장소만 선택 &rarr; 권한
      <code>Actions: Read and write</code> 부여 &rarr; 생성한 토큰을 여기 붙여넣기.</div>
    </details>
  </div>
</div>
<script>
  var REPO="__REPO__", WF="lookup.yml";
  var pat=document.getElementById('pat'), tk=document.getElementById('tk'),
      st=document.getElementById('st'), go=document.getElementById('go');
  pat.value=localStorage.getItem('ghpat')||"";
  function show(m){st.className='stat on';st.innerHTML=m;}
  // 형식 검증: 미국 영문 티커(1~6자) 또는 한국 6자리 코드만. 한글/오타는 수집 전에 거른다.
  var US_RE=/^[A-Z][A-Z.\-]{0,5}$/, KR_RE=/^[0-9]{6}$/, HANGUL=/[ㄱ-ㅣ가-힣]/;
  function classify(v){
    v=(v||'').trim().toUpperCase();
    if(!v) return {ok:null,msg:''};
    if(HANGUL.test(v)) return {ok:false,msg:'❌ 한글은 안 돼요 — 영문 티커(예: AAPL) 또는 한국 6자리 코드(예: 005930)'};
    if(KR_RE.test(v)) return {ok:true,t:v,msg:'✅ 한국 코드 '+v+' — 수집하면 실제 종목명이 확인돼요'};
    if(US_RE.test(v)) return {ok:true,t:v,msg:'✅ 미국 티커 '+v+' — 수집하면 실제 종목명이 확인돼요'};
    return {ok:false,msg:'❌ 형식 오류 — 미국 티커는 영문 1~6자, 한국은 숫자 6자리. ("'+v+'"는 종목 코드가 아니에요)'};
  }
  function validate(v){
    var c=classify(v), el=document.getElementById('tkchk');
    el.className='tkchk'+(c.ok===true?' ok':(c.ok===false?' bad':''));
    el.textContent=c.msg;
    go.disabled=(c.ok===false);
    return c;
  }
  // 즉석조회 누적 이력(브라우저 저장) — 조회한 종목이 목록으로 쌓이고, 수집중은 ⏳
  var HIST=[]; try{HIST=JSON.parse(localStorage.getItem('lookup_hist')||'[]');}catch(e){HIST=[];}
  function saveHist(){localStorage.setItem('lookup_hist',JSON.stringify(HIST.slice(0,40)));}
  function pushHist(t){HIST=HIST.filter(function(h){return h.t!==t;});
    HIST.unshift({t:t,done:false,ts:Date.now()});saveHist();renderHist();}
  function markDone(t,ok){for(var i=0;i<HIST.length;i++){if(HIST[i].t===t){HIST[i].done=true;HIST[i].ok=ok!==false;}}saveHist();renderHist();}
  function delHist(t){HIST=HIST.filter(function(h){return h.t!==t;});saveHist();renderHist();}
  function clearHist(){if(confirm('즉석조회 목록을 비울까요?')){HIST=[];saveHist();renderHist();}}
  function renderHist(){
    var c=document.getElementById('histcard'),h=document.getElementById('hist');
    document.getElementById('histn').textContent=HIST.length?'('+HIST.length+')':'';
    if(!HIST.length){c.style.display='none';return;} c.style.display='';
    h.innerHTML=HIST.map(function(x){
      var s=x.done?(x.ok===false?'⚠️ 실패':'✅ 완료'):'⏳ 수집중';
      return '<div class="hist-item"><a class="t" href="stocks/'+x.t+'.html">'+x.t+'</a>'
        +'<span class="s">'+s+' · <a href="stocks/'+x.t+'.html" style="color:#1d4ed8">상세</a></span>'
        +'<span class="x" title="삭제" onclick="delHist(\\''+x.t+'\\')">&times;</span></div>';
    }).join('');
  }
  function bar(p,c,anim){var pb=document.getElementById('pb'),pf=document.getElementById('pf');
    pb.className='pbar on';pf.style.width=p+'%';if(c)pf.style.background=c;
    pf.className='pfill'+(anim?' anim':'');}
  async function defaultBranch(){
    try{var r=await fetch('https://api.github.com/repos/'+REPO);
      return (await r.json()).default_branch||'main';}catch(e){return 'main';}
  }
  async function run(){
    var t=tk.value.trim().toUpperCase(), p=pat.value.trim();
    if(!t){show('티커를 입력하세요.');return;}
    var c=validate(t);                       // 수집 전 형식 검증(한글/오타 차단)
    if(c.ok===false){show(c.msg);return;}
    if(!p){show('먼저 아래 토큰을 입력하세요(최초 1회).');return;}
    localStorage.setItem('ghpat',p);
    // 이미 진행 중인 같은 종목이면 중복 실행하지 말고 이어서 추적(게이지 초기화 방지)
    var act=loadActive();
    if(act&&act.t===t){
      show('⏳ <b>'+t+'</b> 이미 수집 중 — 이어서 확인합니다.'); bar(45,null,true);
      track(p,t); return;
    }
    go.disabled=true; show('실행 요청 중...'); bar(12,null,true);
    var ref=await defaultBranch();
    var r=await fetch('https://api.github.com/repos/'+REPO+'/actions/workflows/'+WF+'/dispatches',{
      method:'POST',
      headers:{'Authorization':'Bearer '+p,'Accept':'application/vnd.github+json',
        'X-GitHub-Api-Version':'2022-11-28'},
      body:JSON.stringify({ref:ref,inputs:{ticker:t}})
    });
    go.disabled=false;
    if(r.status===204){
      saveActive(t); pushHist(t);   // 진행 상태 저장 + 이력에 추가(수집중)
      show('✅ <b>'+t+'</b> 수집 시작! 1~2분 후 결과를 봅니다...'); bar(30,null,true);
      setTimeout(function(){startTrack(p,t);},6000);
    }else{
      var e=await r.text();
      show('❌ 실패 ('+r.status+'). 토큰/권한 확인.<br><small>'+e.slice(0,160)+'</small>'); bar(100,'#dc2626',false);
    }
  }
  // 추적 루프는 한 번에 하나만(모바일 백그라운드 복귀 시 중복 방지) — 세대 토큰으로 관리
  var TGEN=0;
  function startTrack(p,t){ TGEN++; track(p,t,TGEN); }
  async function track(p,t,gen){
    if(gen!==TGEN)return;            // 더 최신 추적이 시작됐으면 이 루프는 종료
    try{
      var r=await fetch('https://api.github.com/repos/'+REPO+'/actions/workflows/'+WF+'/runs?per_page=1',
        {headers:{'Authorization':'Bearer '+p,'Accept':'application/vnd.github+json'}});
      var run=(await r.json()).workflow_runs[0];
      if(!run){show('실행 대기 중...');bar(35,null,true);setTimeout(function(){track(p,t,gen);},5000);return;}
      if(run.status!=='completed'){
        show('⏳ <b>'+t+'</b> 수집·분석 중... ('+run.status+') · <a href="'+run.html_url+'" target=_blank>로그</a>');
        bar(run.status==='queued'?45:75,null,true);
        setTimeout(function(){track(p,t,gen);},5000);
      }else{
        TGEN++;          // 완료 → 진행 중인 루프 모두 무효화
        clearActive();   // 끝났으니 진행 상태 해제
        var ok=run.conclusion==='success';
        markDone(t,ok);  // 이력 상태 갱신(완료/실패)
        bar(100,ok?'#16a34a':'#dc2626',false);
        show((ok?'✅ 완료! <b>'+t+'</b> 스크리너에 추가됨':'⚠️ '+run.conclusion+' <b>'+t+'</b>')+'<br>'
          +'<a href="index.html">스크리너에서 보기 &rarr;</a> · '
          +'<a href="stocks/'+t+'.html">상세 차트 &rarr;</a> · '
          +'<a href="'+run.html_url+'" target=_blank>로그</a>'
          +'<div class=hint>안 보이면 새로고침(배포 반영 1~2분).</div>');
      }
    }catch(e){show('상태 조회 오류: '+e);}
  }
  // 진행 상태를 브라우저에 저장 → 새로고침/재방문해도 게이지·상태 유지(초기화 방지)
  function saveActive(t){localStorage.setItem('lookup_active',JSON.stringify({t:t,ts:Date.now()}));}
  function clearActive(){localStorage.removeItem('lookup_active');}
  function loadActive(){
    var a=localStorage.getItem('lookup_active'); if(!a)return null;
    try{a=JSON.parse(a);}catch(e){clearActive();return null;}
    if(Date.now()-a.ts>20*60*1000){clearActive();return null;}  // 20분 지나면 만료
    return a;
  }
  tk.addEventListener('keydown',function(e){if(e.key==='Enter')run();});
  // 진행 중이던 조회가 있으면 이어서 추적(모바일은 백그라운드에서 타이머가 멈추므로
  // 화면 복귀(visibilitychange/focus) 때마다 다시 살린다 → 게이지가 안 뜨던 문제 해결)
  function resume(){
    var a=loadActive(); if(!a)return;
    var p=localStorage.getItem('ghpat')||'';
    if(tk)tk.value=a.t;
    show('⏳ <b>'+a.t+'</b> 진행 확인 중...'); bar(40,null,true);
    if(p)startTrack(p,a.t);
    else show('토큰을 입력하면 <b>'+a.t+'</b> 진행 상태를 이어볼 수 있어요.');
  }
  window.addEventListener('load',function(){renderHist();resume();});
  window.addEventListener('focus',resume);
  document.addEventListener('visibilitychange',function(){if(!document.hidden)resume();});
</script></body></html>"""


_PAPER_TMPL = """<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>모의투자</title>
<style>
  body{margin:0;font-family:-apple-system,'Segoe UI','Noto Sans KR',sans-serif;
    background:#f1f5f9;color:#1e293b}
  header{background:#0f172a;color:#fff;padding:13px 18px}
  header a{color:#7dd3fc;font-size:13px;text-decoration:none}
  header h1{margin:4px 0 0;font-size:17px}
  .wrap{max-width:720px;margin:0 auto;padding:14px}
  .card{background:#fff;border:1px solid #e2e8f0;border-radius:13px;padding:15px;margin-bottom:13px}
  .sumv{font-size:30px;font-weight:800;letter-spacing:-.5px}
  .sub{font-size:13px;color:#64748b;margin-top:5px;line-height:1.7}
  .up{color:#16a34a}.dn{color:#dc2626}
  h2{font-size:15px;margin:2px 0 10px;color:#0f172a;display:flex;align-items:center;gap:7px}
  h2 .cnt{font-size:12px;color:#94a3b8;font-weight:600}
  .pick{border:1px solid #e2e8f0;border-radius:11px;padding:11px 12px;margin-bottom:9px}
  .pick.now{border-left:4px solid #16a34a}
  .pick.watch{border-left:4px solid #f59e0b}
  .pick .ph{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
  .pick .nm{font-size:15px;font-weight:700}
  .pick .nm a{color:#0f172a;text-decoration:none}
  .pick .tag{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;background:#f1f5f9;color:#475569}
  .pick .vd{font-size:12.5px;font-weight:800;margin:7px 0 4px}
  .pick .th{font-size:12.5px;color:#334155;line-height:1.65}
  .pick .lv{font-size:12px;color:#475569;margin-top:7px;display:flex;gap:12px;flex-wrap:wrap}
  .pick .lv b{color:#0f172a}
  .pick .buy{margin-top:9px;width:100%;padding:9px;border:0;border-radius:9px;
    background:#15803d;color:#fff;font-size:14px;font-weight:700;cursor:pointer}
  .pick.watch .buy{background:#fff;color:#b45309;border:1px solid #fcd34d}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:8px 6px;border-bottom:1px solid #eef2f7;text-align:right}
  th{color:#64748b;font-size:11px;font-weight:700}
  td.l,th.l{text-align:left}
  .sell{padding:5px 10px;border:1px solid #fca5a5;background:#fff;color:#dc2626;
    border-radius:7px;font-size:12px;font-weight:700;cursor:pointer}
  .srch input{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #cbd5e1;
    border-radius:9px;font-size:16px}
  .res{margin-top:8px} .res .r{display:flex;align-items:center;gap:8px;padding:7px 4px;
    border-bottom:1px solid #f1f5f9;font-size:13px}
  .res .r .bu{margin-left:auto;padding:5px 12px;border:0;border-radius:7px;background:#15803d;
    color:#fff;font-weight:700;cursor:pointer;font-size:12px}
  .logi{font-size:12.5px;padding:6px 2px;border-bottom:1px solid #f1f5f9;display:flex;gap:8px}
  .logi .b{color:#16a34a;font-weight:700}.logi .s{color:#dc2626;font-weight:700}
  .logi .tm{color:#94a3b8;margin-left:auto;font-size:11px}
  .muted{color:#94a3b8;font-size:12.5px;text-align:center;padding:14px}
  .rst{font-size:12px;color:#94a3b8;cursor:pointer;text-decoration:underline}
  .warn{font-size:11.5px;color:#92400e;background:#fffbeb;border:1px solid #fde68a;
    border-radius:9px;padding:9px 11px;margin-bottom:13px;line-height:1.6}
  .flag{font-size:12px;margin-right:2px}
</style></head><body>
<header><a href="index.html">&larr; 스크리너</a>
<h1>💰 모의투자 <span style="color:#86efac;font-size:13px">가상자금 페이퍼 트레이딩</span></h1></header>
<div class="wrap">
  <div class="warn">⚠️ <b>가상 거래</b>입니다(실제 주문 아님). 가격은 <b>30분마다 갱신되는 종가 기준</b>(실시간 아님)이고,
    해외주는 <b>환율 __FX__원/$ 가정</b>으로 원화 환산해요. 데이터는 이 브라우저에만 저장됩니다.</div>

  <div class="card">
    <div class="sumv" id="sumv">–</div>
    <div class="sub" id="sumsub"></div>
    <div style="text-align:right;margin-top:6px"><span class="rst" onclick="reset()">초기화(1,000만원)</span></div>
  </div>

  <div class="card">
    <h2>📈 수익률 추이</h2>
    <div id="chart"></div>
  </div>

  <div class="card">
    <h2>💡 추천 — 지금 진입 검토 <span class="cnt" id="nown"></span></h2>
    <div id="picksNow"></div>
    <h2 style="margin-top:14px">👀 곧 올 자리 · 전환 임박 (관찰) <span class="cnt" id="watchn"></span></h2>
    <div id="picksWatch"></div>
  </div>

  <div class="card">
    <h2>📊 보유 종목</h2>
    <div id="holds"></div>
  </div>

  <div class="card srch">
    <h2>🛒 직접 매수 (종목 검색)</h2>
    <input id="q" type="search" placeholder="🔍 티커·종목명 (예: 삼성, AAPL)" oninput="search(this.value)">
    <div class="res" id="res"></div>
  </div>

  <div class="card">
    <h2>🧾 매매일지</h2>
    <div id="log"></div>
  </div>

  <div class="card">
    <h2>💾 백업 / 복원 <span class="cnt">다른 기기로 옮기기</span></h2>
    <div class="sub" style="margin:0 0 8px">서버 없이 여러 기기에서 쓰는 법: 이 기기에서 <b>백업코드 복사</b> → 다른 기기 모의투자 페이지에서 <b>붙여넣고 복원</b>.</div>
    <textarea id="ioarea" rows="3" style="width:100%;box-sizing:border-box;border:1px solid #cbd5e1;border-radius:8px;padding:9px;font-size:12px" placeholder="백업코드가 여기 표시됩니다 / 복원할 코드를 여기 붙여넣으세요"></textarea>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button onclick="exportData()" style="flex:1;padding:10px;border:0;border-radius:8px;background:#334155;color:#fff;font-weight:700;cursor:pointer">📤 백업코드 복사</button>
      <button onclick="importData()" style="flex:1;padding:10px;border:1px solid #cbd5e1;border-radius:8px;background:#fff;font-weight:700;cursor:pointer">📥 복원</button>
    </div>
  </div>
</div>
<script>
  var FX=__FX__, PRICES=__PRICES__, PICKS=__PICKS__, START=10000000;
  function load(){var a;try{a=JSON.parse(localStorage.getItem('paper_v1'));}catch(e){a=null;}
    if(!a||!a.pos){a={cash:START,start:START,pos:{},log:[]};}return a;}
  function save(a){localStorage.setItem('paper_v1',JSON.stringify(a));}
  function won(v){return Math.round(v).toLocaleString('ko-KR')+'원';}
  function fmtP(p,c){return c==='USD'?('$'+p.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})):(Math.round(p).toLocaleString('ko-KR')+'원');}
  function krw(p,c){return c==='USD'?p*FX:p;}
  function flag(c){return c==='KRW'?'🇰🇷':'🇺🇸';}
  function curPrice(code,fallback){var s=PRICES[code];return s?s[1]:fallback;}

  function render(){
    var a=load();
    var stock=0;
    for(var code in a.pos){var p=a.pos[code];stock+=krw(curPrice(code,p.avg),p.ccy)*p.q;}
    var total=a.cash+stock, pl=total-a.start, pct=a.start?pl/a.start*100:0;
    var cls=pl>=0?'up':'dn', sign=pl>=0?'+':'';
    document.getElementById('sumv').innerHTML=won(total)+' <span class="'+cls+'" style="font-size:18px">'+sign+pct.toFixed(2)+'%</span>';
    document.getElementById('sumsub').innerHTML='현금 '+won(a.cash)+' · 주식 '+won(stock)
      +' · 평가손익 <span class="'+cls+'">'+sign+won(pl)+'</span> · 시작금 '+won(a.start);
    renderHolds(a); renderLog(a); renderChart(snapshot(total));
  }
  // 수익률 추이: 방문할 때마다 하루 1점씩 총평가액 스냅샷 저장 → equity curve
  function loadHist(){try{return JSON.parse(localStorage.getItem('paper_hist'))||[];}catch(e){return [];}}
  function snapshot(total){
    var h=loadHist(), today=new Date().toISOString().slice(0,10);
    if(h.length&&h[h.length-1].d===today){h[h.length-1].v=total;}
    else{h.push({d:today,v:total});}
    if(h.length>180)h=h.slice(-180);
    localStorage.setItem('paper_hist',JSON.stringify(h));
    return h;
  }
  function renderChart(h){
    var el=document.getElementById('chart');
    if(!h||h.length<2){el.innerHTML='<div class="muted">매일 방문하면 점이 하루 1개씩 쌓여 수익률 곡선이 그려져요 (현재 '+((h&&h.length)||1)+'일째).</div>';return;}
    var vs=h.map(function(x){return x.v;});
    var mn=Math.min.apply(null,vs.concat([START])), mx=Math.max.apply(null,vs.concat([START]));
    var W=600,H=140,pad=10,rng=(mx-mn)||1;
    function X(i){return pad+i*(W-2*pad)/(h.length-1);}
    function Y(v){return pad+(H-2*pad)*(1-(v-mn)/rng);}
    var pts=h.map(function(x,i){return X(i).toFixed(1)+','+Y(x.v).toFixed(1);}).join(' ');
    var last=vs[vs.length-1], up=last>=START, col=up?'#16a34a':'#dc2626', bY=Y(START).toFixed(1);
    el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" style="width:100%;height:140px">'
      +'<line x1="'+pad+'" y1="'+bY+'" x2="'+(W-pad)+'" y2="'+bY+'" stroke="#cbd5e1" stroke-dasharray="4 4"/>'
      +'<polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="2" stroke-linejoin="round"/></svg>'
      +'<div class="sub">최근 '+h.length+'일 · 점선=시작금 '+won(START)+'</div>';
  }
  // 백업/복원: 서버 없이 다른 기기로 이전(base64로 직렬화)
  function exportData(){
    var blob={a:load(),h:loadHist()};
    var s=btoa(unescape(encodeURIComponent(JSON.stringify(blob))));
    var t=document.getElementById('ioarea'); t.value=s; t.focus(); t.select();
    try{document.execCommand('copy');}catch(e){}
    alert('백업코드를 복사했어요. 다른 기기 모의투자 페이지의 복원칸에 붙여넣고 "복원"을 누르세요.');
  }
  function importData(){
    var s=(document.getElementById('ioarea').value||'').trim(); if(!s){alert('복원할 백업코드를 붙여넣으세요.');return;}
    try{var blob=JSON.parse(decodeURIComponent(escape(atob(s))));
      if(blob.a&&blob.a.pos)save(blob.a);
      if(blob.h)localStorage.setItem('paper_hist',JSON.stringify(blob.h));
      render(); alert('복원 완료!');
    }catch(e){alert('복원 실패 — 백업코드가 올바른지 확인하세요.');}
  }
  function renderHolds(a){
    var codes=Object.keys(a.pos), h=document.getElementById('holds');
    if(!codes.length){h.innerHTML='<div class="muted">보유 종목 없음 — 위 추천이나 검색에서 매수해보세요.</div>';return;}
    var rows='<table><tr><th class=l>종목</th><th>수량</th><th>평단</th><th>현재가</th><th>평가손익</th><th></th></tr>';
    codes.forEach(function(code){var p=a.pos[code];var cur=curPrice(code,p.avg);
      var plp=(cur/p.avg-1)*100, plv=(krw(cur,p.ccy)-krw(p.avg,p.ccy))*p.q;
      var cls=plv>=0?'up':'dn',sg=plv>=0?'+':'';
      rows+='<tr><td class=l><span class=flag>'+flag(p.ccy)+'</span><a href="stocks/'+code+'.html" style="color:#1d4ed8;text-decoration:none">'+p.name+'</a></td>'
        +'<td>'+p.q+'</td><td>'+fmtP(p.avg,p.ccy)+'</td><td>'+fmtP(cur,p.ccy)+'</td>'
        +'<td class="'+cls+'">'+sg+won(plv)+'<br><span style=font-size:11px>'+sg+plp.toFixed(1)+'%</span></td>'
        +'<td><button class=sell onclick="sellP(\\''+code+'\\')">매도</button></td></tr>';
    });
    h.innerHTML=rows+'</table>';
  }
  function renderLog(a){
    var l=document.getElementById('log');
    if(!a.log.length){l.innerHTML='<div class="muted">거래 내역 없음.</div>';return;}
    l.innerHTML=a.log.slice(0,40).map(function(x){
      var d=new Date(x.t), ds=(d.getMonth()+1)+'/'+d.getDate()+' '+('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);
      return '<div class=logi><span class="'+(x.type==='buy'?'b':'s')+'">'+(x.type==='buy'?'매수':'매도')+'</span>'
        +'<span>'+flag(x.ccy)+' '+x.name+' '+x.q+'주 @ '+fmtP(x.price,x.ccy)+'</span>'
        +'<span class=tm>'+won(x.amt)+' · '+ds+'</span></div>';
    }).join('');
  }
  function pickCard(p,kind){
    var lv='<b>진입</b> '+fmtP(p.entry,p.ccy)+' · <b>손절</b> '+fmtP(p.stop,p.ccy)+' · <b>목표</b> '+fmtP(p.target,p.ccy);
    var vc=p.verdict.indexOf('타당')>=0?'up':(p.verdict.indexOf('회피')>=0?'dn':'');
    return '<div class="pick '+kind+'"><div class=ph><span class=flag>'+flag(p.ccy)+'</span>'
      +'<span class=nm><a href="stocks/'+p.code+'.html">'+p.name+'</a></span>'
      +'<span class=tag>'+p.sig+'</span>'+(p.stage?'<span class=tag>'+p.stage+'</span>':'')+'</div>'
      +'<div class="vd '+vc+'">'+p.verdict+'</div>'
      +'<div class=th>🤖 클로드라면: '+p.thesis+'</div>'
      +'<div class=lv>'+lv+'</div>'
      +'<button class=buy onclick="buyP(\\''+p.code+'\\')">'+(kind==='now'?'💰 매수':'💰 그래도 매수')+'</button></div>';
  }
  function renderPicks(){
    document.getElementById('picksNow').innerHTML=PICKS.now.length?PICKS.now.map(function(p){return pickCard(p,'now');}).join(''):'<div class=muted>지금 진입 타당한 종목 없음(시장 하락 등). 관찰 목록을 보세요.</div>';
    document.getElementById('picksWatch').innerHTML=PICKS.watch.length?PICKS.watch.map(function(p){return pickCard(p,'watch');}).join(''):'<div class=muted>관찰 대상 없음.</div>';
    document.getElementById('nown').textContent=PICKS.now.length+'개';
    document.getElementById('watchn').textContent=PICKS.watch.length+'개';
  }
  function buyP(code){
    var s=PRICES[code]; if(!s){alert('가격 정보 없음');return;}
    var name=s[0],price=s[1],ccy=s[2], per=krw(price,ccy);
    var a=load();
    var max=Math.floor(a.cash/per);
    var v=prompt(name+' 매수\\n현재가 '+fmtP(price,ccy)+' (1주 ≈ '+won(per)+')\\n현금 '+won(a.cash)+' → 최대 '+max+'주\\n\\n살 수량:', Math.min(max,1)||'');
    if(!v)return; var q=parseInt(v); if(!(q>0)){return;}
    var cost=Math.round(per*q);
    if(cost>a.cash){alert('현금 부족: 필요 '+won(cost)+' / 보유 '+won(a.cash));return;}
    a.cash-=cost;
    var p=a.pos[code]||{q:0,avg:0,ccy:ccy,name:name};
    p.avg=(p.avg*p.q+price*q)/(p.q+q); p.q+=q; p.name=name; a.pos[code]=p;
    a.log.unshift({t:Date.now(),type:'buy',code:code,name:name,q:q,price:price,ccy:ccy,amt:cost});
    save(a); render();
  }
  function sellP(code){
    var a=load(); var p=a.pos[code]; if(!p)return;
    var price=curPrice(code,p.avg), per=krw(price,p.ccy);
    var v=prompt(p.name+' 매도\\n보유 '+p.q+'주 · 현재가 '+fmtP(price,p.ccy)+'\\n\\n팔 수량(비우면 전량):', p.q);
    if(v===null)return; var q=v?parseInt(v):p.q; if(!(q>0)||q>p.q){return;}
    var proceeds=Math.round(per*q);
    a.cash+=proceeds; p.q-=q; if(p.q<=0)delete a.pos[code];
    a.log.unshift({t:Date.now(),type:'sell',code:code,name:p.name,q:q,price:price,ccy:p.ccy,amt:proceeds});
    save(a); render();
  }
  function search(v){
    v=(v||'').trim().toLowerCase(); var res=document.getElementById('res');
    if(!v){res.innerHTML='';return;}
    var hits=[],n=0;
    for(var code in PRICES){var s=PRICES[code];
      if(code.toLowerCase().indexOf(v)>=0||s[0].toLowerCase().indexOf(v)>=0){
        hits.push('<div class=r><span class=flag>'+flag(s[2])+'</span><a href="stocks/'+code+'.html" style="color:#1d4ed8;text-decoration:none">'+s[0]+'</a> <span style=color:#94a3b8;font-size:11px>'+code+' · '+fmtP(s[1],s[2])+'</span><button class=bu onclick="buyP(\\''+code+'\\')">매수</button></div>');
        if(++n>=20)break;}}
    res.innerHTML=hits.length?hits.join(''):'<div class=muted>검색 결과 없음</div>';
  }
  function reset(){if(confirm('모의투자를 초기화할까요? (보유·현금·일지 모두 삭제, 1,000만원으로)')){save({cash:START,start:START,pos:{},log:[]});render();}}
  renderPicks(); render();
</script></body></html>"""


_GUIDE_HTML = """<!DOCTYPE html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>매매 가이드</title>
<style>
  body{margin:0;font-family:-apple-system,'Segoe UI','Noto Sans KR',sans-serif;
    background:#f1f5f9;color:#1e293b;line-height:1.7}
  header{background:#0f172a;color:#fff;padding:14px 20px}
  header a{color:#7dd3fc;font-size:13px;text-decoration:none}
  header h1{margin:5px 0 0;font-size:18px}
  .wrap{max-width:760px;margin:0 auto;padding:16px}
  .card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px 18px;margin-bottom:14px}
  h2{font-size:16px;margin:2px 0 10px;color:#0f172a}
  h2 .em{color:#2563eb}
  table{width:100%;border-collapse:collapse;font-size:13.5px;margin:6px 0}
  th,td{border-bottom:1px solid #eef2f7;padding:7px 8px;text-align:left;vertical-align:top}
  th{color:#64748b;font-size:12px;background:#f8fafc}
  .good{color:#16a34a;font-weight:700} .bad{color:#dc2626;font-weight:700}
  .warn{color:#d97706;font-weight:700}
  ul{margin:6px 0;padding-left:20px} li{margin:3px 0}
  .pill{display:inline-block;background:#eff6ff;color:#1d4ed8;border-radius:999px;
    padding:1px 9px;font-size:12px;font-weight:600;margin-right:4px}
  .note{font-size:12.5px;color:#64748b}
  .big{background:#0f172a;color:#e2e8f0;border:0}
  .big b{color:#7dd3fc}
</style></head><body>
<header><a href="index.html">&larr; 스크리너</a>
<h1>📘 매매 가이드 — 이 차트를 어떻게 쓰나</h1></header>
<div class="wrap">

<div class="card big">
<b>핵심 철학:</b> 이건 <b>"강세 추격기"가 아니라 "하락→상승 전환 포착기"</b>입니다.
신호가 🟢강세(+50↑)일 때가 흔히 제일 안 좋은 자리(이미 다 오른 추격권)예요.
<br>한 줄: <b>남들이 무서워서 못 살 때(전환 초입) 사고, 모두가 강세라 외칠 때(추격) 사지 않는다.</b>
<br><span class="note">적중률은 절반 안팎. 돈은 "손익비"로 법니다 — 틀리면 −1R, 맞으면 +2~3R.
이 도구의 가장 큰 가치는 <b>크게 잃을 자리를 걸러주는 것</b>입니다.</span>
</div>

<div class="card">
<h2>컬럼별 <span class="em">좋은 값</span></h2>
<table>
<tr><th>컬럼</th><th>뭘 보나</th><th>좋은 신호</th></tr>
<tr><td>전환단계</td><td>하락→상승 전환 진행도</td><td class="good">③ 돌파후 횡보 · ④ 전환 확정 ⭐</td></tr>
<tr><td>신호</td><td>종합점수 −100~+100</td><td>🟢관심(+20~50)이 스윗스팟. 🟢강세는 추격 주의</td></tr>
<tr><td>RS</td><td>지수 대비 상대강도</td><td class="good">+(플러스) & 상승 중</td></tr>
<tr><td>신고가</td><td>52주 고가 대비 %</td><td>−20%~−3%(근접하되 안 붙음). <span class="bad">0%는 추격권</span></td></tr>
<tr><td>시장</td><td>지수 방향</td><td class="good">상승</td> </tr>
</table>
</div>

<div class="card">
<h2>전환단계의 <span class="em">뜻과 기준</span></h2>
<ul>
<li><b>④ 전환 확정 ⭐</b> — ①하락추세선을 <b>거래량 동반 돌파</b> → ②되눌림 후 <b>안착</b> → ③위로 <b>상승추세선</b>까지 형성. 셋 다 충족된 가장 강한 신호.</li>
<li><b>③ 돌파후 횡보(대기)</b> — 추세선은 넘었고 위에서 다지는 중. 상승추세선·거래량 확인되면 ④로.</li>
<li><b>② 갓 돌파(미확인)</b> — 막 넘었지만 되밀릴 수 있어 안착 미확인 → 관망.</li>
<li><b>① 임박</b> — 아직 추세선 아래지만 저항 근접. 돌파하면 전환 시작.</li>
</ul>
</div>

<div class="card">
<h2>✅ "내가 산다면" <span class="em">체크리스트</span></h2>
<ul>
<li><span class="pill">1</span>전환단계 <b>③ 또는 ④</b></li>
<li><span class="pill">2</span>시장 = <b>상승</b> (지수가 받쳐줄 때만)</li>
<li><span class="pill">3</span>RS <b>플러스</b> (시장보다 강함)</li>
<li><span class="pill">4</span>신고가 <b>−20%~−3%</b> (고가 근접, 아직 안 붙음)</li>
<li><span class="pill">5</span><b>🔺추격주의 없음</b> (과열·과대이격 아님)</li>
<li><span class="pill">6</span>신호 <b>🟢관심 이상</b></li>
</ul>
<span class="note">1·2·3 충족 = 진지하게 검토 · 1~6 다 충족 = 1순위 매수 후보.</span>
</div>

<div class="card">
<h2>"중립 vs 강세" <span class="em">어디서 사나</span></h2>
<ul>
<li><span class="good">⚪중립 + 전환단계 ③④</span> → 👍 가장 좋은 자리(선취매 구간).</li>
<li><span class="bad">🟢강세 + 전환 0 + 신고가 0% + 추격주의</span> → ⚠️ 추격. 눌림 대기.</li>
<li>⚪중립 + 전환 0 → 관망(살 이유 없음).</li>
<li><span class="bad">🔴주의/공포 · 🔴하락추세</span> → 매수 금지. 보유 중이면 손절 점검.</li>
</ul>
</div>

<div class="card">
<h2>진입 · 손절 · 목표 <span class="em">(리스크 관리가 본체)</span></h2>
<ul>
<li><b>진입</b>: 차트 "진입선". 고점권이면 <b>저항 돌파 확인 후</b>, 아니면 현재가 분할.</li>
<li><b>손절</b>: "손절선"(진입−2×ATR) <b class="bad">밑으로 종가 마감 시 무조건 정리.</b> ← 안 지키면 도구 의미 없음.</li>
<li><b>목표</b>: "목표선"(손익비 1:2). 절반 도달 시 일부 익절 + 손절 올리기.</li>
<li><b>비중</b>: 한 종목 손실이 전체의 <b>1~2%</b>를 넘지 않게.</li>
</ul>
</div>

<div class="card">
<h2>피해야 할 것 · <span class="em">한계</span></h2>
<ul>
<li class="bad">🔺추격주의 종목 신규 진입 / 🔴하락추세 "쌀 것 같아서" 받기 / 시장 하락에 매수.</li>
<li><span class="warn">📅어닝 D-3 이내</span> 종목은 갭 리스크로 신규 진입 회피.</li>
<li class="note">차트만 보므로 실적·뉴스·갭은 못 봅니다. 정밀한 분 단위 진입은 증권사 앱으로.
 백테스트 엣지는 작고(+0.1R 수준) 생존편향이 있어 "예측기"가 아닌 "리스크 도구"로 쓰세요.</li>
</ul>
</div>

<p class="note" style="text-align:center">이 페이지는 일반적 가이드이며 투자 권유가 아닙니다. 최종 판단·책임은 본인에게 있습니다.</p>
</div></body></html>"""
