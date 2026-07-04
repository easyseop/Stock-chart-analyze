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

# HTML 템플릿은 templates/ 폴더의 별도 파일 — 파이썬 문자열 내 JS/CSS 이스케이프
# 버그(과거 delHist \\' 2회, f-string 충돌 1회)의 원인을 제거. 치환은 __TOKEN__ 방식.
_TMPL_DIR = os.path.join(os.path.dirname(__file__), "templates")


def _tmpl(name: str) -> str:
    with open(os.path.join(_TMPL_DIR, name), encoding="utf-8") as fp:
        return fp.read()

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
                          thesis as _plan_thesis, tactic as _plan_tactic)


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
        rp_pct = float(r.get("range_pos", 0.5)) * 100     # 52주 범위 내 위치(낮을수록 저점)
        rp_cls = "pos" if rp_pct <= 40 else ("neg" if rp_pct >= 60 else "")
        ccy = r.get("ccy", "USD")
        # 토스식 리스트: 현재가 + 전일 대비 등락(한국식 색 — 상승 빨강/하락 파랑)
        chg = float(r.get("day_chg", 0)) * 100
        px_txt = (f"{price:,.0f}원" if ccy == "KRW" else f"${price:,.2f}") if price else "-"
        chg_txt = f"{chg:+.2f}%"
        chg_cls = "pos" if chg > 0 else ("neg" if chg < 0 else "")
        region = "kr" if ccy == "KRW" else "us"
        flag = "🇰🇷" if region == "kr" else "🇺🇸"
        out.append(
            f'<tr class="b-{b}{" rec" if recd else ""}" data-bucket="{b}" '
            f'data-stage="{stg}" data-rec="{rec}" data-code="{code}" '
            f'data-price="{price_attr}" data-ccy="{ccy}" data-region="{region}">'
            f'<td data-label="신호" title="{gtip}">'
            f'<span class="sig">{star}{html.escape(gauge)}</span></td>'
            f'<td class="nm"><button class="hold" onclick="toggleHold(event,\'{code}\')" '
            f'title="내 종목(매수) 담기" aria-label="관심 추가" aria-pressed="false">☆</button>'
            f'<span class="rgn" title="{"국내(한국)" if region=="kr" else "해외(미국)"}">{flag}</span>'
            f'<a href="stocks/{code}.html">{html.escape(r["name"])}</a>{ko_html}'
            f'<span class="cd">{html.escape(code)}</span>'
            f'<span class="pl" data-code="{code}"></span>'
            f'<span class="pxm"><b class="pxv">{px_txt}</b>'
            f'<span class="pxc {chg_cls}">{chg_txt}</span></span></td>'
            f'<td data-label="전환단계" data-v="{stg}" class="num stg" '
            f'title="{stip}">{stg_lab}</td>'
            f'<td data-label="추세">{tone}</td>'
            f'<td data-label="점수" data-v="{r["norm"]:.1f}" '
            f'class="num sc {_sign(r["norm"])}">{r["norm"]:+.0f}</td>'
            f'<td data-label="시장"><span class="{_mkt_cls(mk)}">{html.escape(mk)}</span></td>'
            f'<td data-label="RS" data-v="{rs if rs is not None else -999}" '
            f'class="num {_sign(rs) if rs is not None else ""}">{rs_txt}</td>'
            f'<td data-label="신고가" data-v="{nh if nh is not None else -999}" class="num">{nh_txt}</td>'
            f'<td data-label="저점권" data-v="{rp_pct}" class="num {rp_cls}">{rp_pct:.0f}%</td>'
            f'<td data-label="판정" class="vd">{vd}</td>'
            f'<td class="brk"></td></tr>')
    return "".join(out)


def _tabbar(active: str) -> str:
    """하단 탭바(모바일) — index/paper/more 공용. active: home/stocks/paper/more."""
    items = [("home", "index.html#home", "🏠", "홈"),
             ("stocks", "index.html#stocks", "📋", "종목"),
             ("paper", "paper.html", "💰", "모의"),
             ("more", "more.html", "☰", "더보기")]
    tabs = "".join(
        f'<a href="{href}" id="tab-{k}" class="tb{" on" if k == active else ""}"'
        f'{" aria-current=page" if k == active else ""}>'
        f'<span class="ti">{ic}</span>{lab}</a>'
        for k, href, ic, lab in items)
    return f'<nav class="tabbar" aria-label="주요 메뉴">{tabs}</nav>'


def _fmt_px(v: float, ccy: str) -> str:
    return f"{v:,.0f}원" if ccy == "KRW" else f"${v:,.2f}"


def _zone_meter(rp: int) -> str:
    """52주 저점권 미터 — 파란 띠(0~40%)=매수 목표 구간, 점=현재 위치."""
    dot = max(2, min(98, int(rp)))
    return (f'<div class="zone"><div class="zl"><span>52주 저점</span>'
            f'<span>지금 {rp}%</span><span>고점</span></div>'
            f'<div class="zbar"><div class="band" style="width:40%"></div>'
            f'<span class="zdot" style="left:{dot}%"></span></div></div>')


def _chg_html(p: dict) -> str:
    chg = p.get("day_chg")
    if chg is None:
        return ""
    cls = "pos" if chg > 0 else ("neg" if chg < 0 else "")
    return f'<span class="{cls}">{chg:+.2f}%</span>'


def _home_card(p: dict) -> str:
    """홈 픽 카드 — 카드 한 장이 결정 한 번: 가격·신선도·저점권·계획·행동."""
    flag = "🇰🇷" if p["ccy"] == "KRW" else "🇺🇸"
    tags = []
    if p.get("freshness"):
        gap = p.get("break_gap")
        gap_txt = f" +{gap}%" if (gap is not None and gap >= 0 and p.get("fresh")) else ""
        tags.append(f'<span class="tg {"hot" if p.get("fresh") else "stale"}">'
                    f'{html.escape(p["freshness"])}{gap_txt}</span>')
    if p.get("stage"):
        tags.append(f'<span class="tg st">{html.escape(p["stage"])}</span>')
    t = p.get("tactic")
    if t:   # 진입 전술 — 카드에서 가장 행동에 가까운 정보라 눈에 띄게
        cls = {"full": "hot", "half": "st", "pullback": "wait"}.get(t["mode"], "")
        tags.append(f'<span class="tg {cls}">{html.escape(t["label"])}</span>')
    tags.append(f'<span class="tg">{html.escape(p["sig"])}</span>')
    fp = lambda v: _fmt_px(v, p["ccy"])
    # 눌림 지정가 전술이면 진입 칸에 '눌림 목표가'를 보여준다 — 지금 사라는 뜻이 아님
    ent_lab, ent_v = "진입", p["entry"]
    if t and t["mode"] == "pullback" and t.get("pb_price"):
        ent_lab, ent_v = "진입(눌림)", t["pb_price"]
    return (
        f'<div class="pk"><div class="top">'
        f'<div class="nm"><a href="stocks/{p["code"]}.html">{flag} {html.escape(p["name"])}</a>'
        f'<span class="cd">{html.escape(p["code"])}</span></div>'
        f'<div class="px"><b>{fp(p["price"]) if p.get("price") else "-"}</b>{_chg_html(p)}</div></div>'
        f'<div class="tags">{"".join(tags)}</div>'
        f'{_zone_meter(p.get("range_pos", 50))}'
        f'<div class="lvl">'
        f'<div class="lv e"><span>{ent_lab}</span><b>{fp(ent_v)}</b></div>'
        f'<div class="lv s"><span>손절</span><b>{fp(p["stop"])}</b></div>'
        f'<div class="lv t"><span>목표</span><b>{fp(p["target"])}</b></div></div>'
        # 카드엔 이유만 — 손절·목표 숫자는 위 3칸에 이미 있어 문장 꼬리를 잘라 중복 제거
        f'<div class="why">🤖 {p["thesis"].split(" 손절 ")[0].rstrip("—- ·,")}</div>'
        f'<div class="cta"><a class="c1" href="stocks/{p["code"]}.html">📈 차트 보기</a>'
        f'<a class="c2" href="paper.html?buy={p["code"]}">💰 모의 매수</a></div></div>')


def _soon_row(p: dict) -> str:
    flag = "🇰🇷" if p["ccy"] == "KRW" else "🇺🇸"
    sub = html.escape(p.get("freshness") or p.get("stage") or p["sig"])
    return (
        f'<a class="sr" href="stocks/{p["code"]}.html">'
        f'<span class="fl">{flag}</span>'
        f'<span class="n"><b>{html.escape(p["name"])}</b><span>{sub}</span></span>'
        f'<span class="zp">저점권 {p.get("range_pos", 50)}%</span>'
        f'<span class="p"><b>{_fmt_px(p["price"], p["ccy"]) if p.get("price") else "-"}</b>'
        f'{_chg_html(p)}</span></a>')


def _mkline(results: list[dict]) -> str:
    """홈 상단 시장 방향 한 줄 — 진입 추천의 전제 조건이라 항상 보이게."""
    parts = []
    for flag, want_krw in (("🇰🇷", True), ("🇺🇸", False)):
        d = next((r.get("market", {}).get("direction", "") for r in results
                  if (r.get("ccy") == "KRW") == want_krw), "")
        if d:
            cls = "pos" if "상승" in d else ("neg" if "하락" in d else "")
            parts.append(f'{flag} 시장 <b class="{cls}">{html.escape(d)}</b>')
    return (" · " + " · ".join(parts)) if parts else ""


_HOME_CARD_MAX = 8     # 홈은 '답'만 — 전체 목록은 종목 탭에서


def _home_html(results: list[dict], tstats: dict | None = None,
               auto: dict | None = None) -> str:
    """홈 = 오늘의 답: 스코어보드 → 지금 진입 카드 → 곧 올 자리 행."""
    picks = _paper_picks(results)
    now, watch = picks["now"], picks["watch"]
    open_n = (tstats or {}).get("open", 0)
    board = (
        f'<div class="board">'
        f'<a class="bcell now" style="text-decoration:none" href="#stocks"><b>{len(now)}</b><span>지금 진입</span></a>'
        f'<a class="bcell watch" style="text-decoration:none" href="#stocks"><b>{len(watch)}</b><span>곧 올 자리</span></a>'
        f'<a class="bcell trk" style="text-decoration:none" href="paper.html"><b>{open_n}</b><span>성과 추적 중</span></a></div>')
    stat = ""
    if auto and auto.get("start"):        # 🤖 자동 모의투자 성과 — 홈에서 한눈에
        rp = auto.get("ret_pct", 0)
        cls = "pos" if rp > 0 else ("neg" if rp < 0 else "")
        stat += (f'<div class="hstat">🤖 자동 모의투자(시드 1억): '
                 f'<b class="{cls}">{rp:+.2f}%</b> · 보유 {len(auto.get("positions", []))}종목 · '
                 f'청산 {auto.get("trades", 0)}회'
                 + (f' · 승률 <b>{auto["stats"]["all"]["win_rate"]}%</b>'
                    if auto.get("stats", {}).get("all", {}).get("n") else "")
                 + ' · <a href="paper.html" style="color:#1d6ce0;font-weight:700">상세 →</a></div>')
    if tstats and tstats.get("closed"):
        stat += (f'<div class="hstat">📊 지난 추천 성과(자동 채점): '
                 f'<b>{tstats["wins"]}승 {tstats["losses"]}패</b> · 승률 '
                 f'<b>{tstats["win_rate"]}%</b> · 평균 <b>{tstats["avg_r"]:+.2f}R</b></div>')
    if now:
        cards = "".join(_home_card(p) for p in now[:_HOME_CARD_MAX])
        rest = (f'<div class="hmuted">외 {len(now) - _HOME_CARD_MAX}개 — '
                f'<a href="#stocks" style="color:#1d6ce0">종목 탭 ⭐추천</a>에서</div>'
                if len(now) > _HOME_CARD_MAX else "")
        now_sec = (f'<div class="hsec">🟢 지금 진입 <span class="cnt">{len(now)}</span>'
                   f'<span class="rmk">전환 확정 · 저점</span></div>{cards}{rest}')
    else:
        now_sec = ('<div class="hsec">🟢 지금 진입 <span class="cnt">0</span></div>'
                   '<div class="hstat">지금 바로 들어갈 전환 확정(저점) 종목이 없어요 — '
                   '보통 <b>시장이 하락</b>일 때예요. 아래 "곧 올 자리"를 관찰하다가 '
                   '시장이 돌면 1순위로 진입.</div>')
    if watch:
        rows = "".join(_soon_row(p) for p in watch[:_HOME_CARD_MAX])
        rest_w = (f'<div class="hmuted">외 {len(watch) - _HOME_CARD_MAX}개 — '
                  f'<a href="#stocks" style="color:#1d6ce0">종목 탭</a>에서</div>'
                  if len(watch) > _HOME_CARD_MAX else "")
        watch_sec = (f'<div class="hsec">👀 곧 올 자리 <span class="cnt">{len(watch)}</span>'
                     f'<span class="rmk">전환 임박 · 눌림 대기</span></div>'
                     f'<div class="soon">{rows}</div>{rest_w}')
    else:
        watch_sec = ""
    return (f'{board}{stat}{now_sec}{watch_sec}'
            f'<div class="hmuted">⚠️ 차트 기준 추천 · 투자권유 아님 · '
            f'<a href="paper.html">💰 모의투자로 연습</a> · '
            f'처음이라면 <a href="start.html" style="color:#1d6ce0">🚀 3분 가이드</a></div>')


def _index(results: list[dict], tstats: dict | None = None,
           auto: dict | None = None) -> tuple[str, str]:
    """(index.html, more.html) 두 페이지를 렌더해 반환."""
    import datetime
    from scanner import cache, universe
    # 기본 정렬: 진입 추천 점수 → 전환 단계 → 종합점수 (추천 종목이 맨 위로)
    results = sorted(results,
                     key=lambda r: (_rec_n(r), r.get("transition_stage", 0), r["norm"]),
                     reverse=True)
    counts = {k: sum(1 for r in results if _bucket(r) == k) for k, _ in _BUCKETS}
    rcount = sum(1 for r in results if _rec_n(r) >= REC_MIN)
    chips = "".join(
        f'<button class="chip" data-f="bucket" data-v="{k}" aria-pressed="false" '
        f'onclick="chip(this)">{_BUCKET_KO[k]} {counts[k]}</button>'
        for k, _ in _BUCKETS)
    # 전환단계 ①~④ 각각 필터
    stage_lab = {1: "①임박", 2: "②갓돌파", 3: "③횡보", 4: "④확정"}
    scnt = {s: sum(1 for r in results if r.get("transition_stage", 0) == s)
            for s in (1, 2, 3, 4)}
    stage_chips = "".join(
        f'<button class="chip stagechip" data-f="stage" data-v="{s}" aria-pressed="false" '
        f'onclick="chip(this)">{stage_lab[s]} {scnt[s]}</button>'
        for s in (1, 2, 3, 4) if scnt[s])
    # 국내(한국)/해외(미국) 구분 필터
    n_us = sum(1 for r in results if r.get("ccy") != "KRW")
    n_kr = sum(1 for r in results if r.get("ccy") == "KRW")
    region_chips = (
        f'<button class="chip rgnchip" data-f="region" data-v="us" aria-pressed="false" '
        f'onclick="chip(this)">🇺🇸 해외 {n_us}</button>'
        f'<button class="chip rgnchip" data-f="region" data-v="kr" aria-pressed="false" '
        f'onclick="chip(this)">🇰🇷 국내 {n_kr}</button>')
    # 수집 진행률(캐시된 종목 / 유니버스 전체)
    try:
        cached = len(cache.cached_codes())
        uni = len([s for s in universe.load() if s.get("code")]) or 1
    except Exception:
        cached, uni = len(results), max(len(results), 1)
    uni = max(uni, cached)     # 즉석조회 추가분으로 수집>유니버스가 될 수 있음 — 표기 꼬임 방지
    pct = min(100, round(cached / uni * 100))
    updated = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    import config
    ts = int(__import__("time").time())            # 클라이언트 'N분 전' 표시용
    tokens = {
        "__ROWS__": _rows(results), "__CHIPS__": chips,
        "__STAGE_CHIPS__": stage_chips, "__REGION_CHIPS__": region_chips,
        "__HOME__": _home_html(results, tstats, auto),
        "__MKLINE__": _mkline(results),
        "__TABBAR__": _tabbar("home"),
        "__RCOUNT__": rcount, "__RECMIN__": REC_MIN,
        "__UPDATED_TS__": ts,
    }
    page = _tmpl("index.html")
    for k, v in tokens.items():
        page = page.replace(k, str(v))
    # 더보기 페이지 — 수집 현황·갱신 안내·범례가 이사한 곳
    more = _tmpl("more.html")
    for k, v in {
        "__CACHED__": cached, "__UNI__": uni, "__PCT__": pct,
        "__UPDATED__": updated, "__UPDATED_TS__": ts,
        # P5: 갱신주기 문구는 config에서 — 크론 바꿀 때 안내문이 저절로 따라오게
        "__INTERVAL__": config.UPDATE_INTERVAL_MIN,
        "__TABBAR__": _tabbar("more"),
    }.items():
        more = more.replace(k, str(v))
    return page, more


def build(results: list[dict], frames_map: dict[str, dict],
          out_dir: str = "public", metas: dict | None = None) -> str:
    """스크리너(index.html) + 종목별 상세(stocks/*.html) 생성. out_dir 반환."""
    import config
    from scanner import gates, track
    # 자가검증(불변식): 모순·잡주·폭등·손절폭 위반이 있으면 여기서 빌드 실패 →
    # 나쁜 추천이 배포되는 일 자체를 막는다(지난 회귀: NXPI·KRC·GPUS·DB하이텍·AES).
    picks = _paper_picks(results)
    gates.audit(results, picks)
    # 추천 성과 자동 채점(포워드 테스트) — 홈 요약 + /api/track.json
    tstats = track.update(results, picks, out_dir)
    # 클로드 자동 모의투자(시드 1억) — 전술대로 스스로 진입·청산 시뮬레이션
    from scanner import autopaper
    auto = autopaper.update(results, picks, out_dir)
    stocks_dir = os.path.join(out_dir, "stocks")
    os.makedirs(stocks_dir, exist_ok=True)
    h_min = config.MA_PERIODS["H"][-1] + 5

    # ── 증분 렌더: 5,400페이지(≈900MB)를 매 15분 전부 다시 그리던 게 빌드 5분·
    #    배포 정체의 뿌리. 데이터(csv)가 페이지보다 새로울 때만 재렌더하고 나머지는
    #    이전 빌드 산출물(actions/cache로 보존) 재사용. 코드가 바뀌면(BUILD_SHA 다름)
    #    전체 재렌더 — 페이지 코드와 데이터가 어긋나는 일 방지.
    from scanner import cache as _dcache
    sha = os.environ.get("BUILD_SHA", "")
    stamp = os.path.join(stocks_dir, ".build_sha")
    prev_sha = None
    if os.path.exists(stamp):
        with open(stamp, encoding="utf-8") as fp:
            prev_sha = fp.read().strip()
    incremental = bool(sha) and prev_sha == sha

    def _fresh(code: str, path: str) -> bool:
        """페이지가 데이터보다 최신이면(재렌더 불필요) True."""
        if not os.path.exists(path):
            return False
        src = _dcache._path(code)
        if not os.path.exists(src):
            return False
        src_m = os.path.getmtime(src)
        hp = os.path.join("data_cache_h", f"{code}.csv.gz")   # 시간봉도 반영
        if os.path.exists(hp):
            src_m = max(src_m, os.path.getmtime(hp))
        return os.path.getmtime(path) >= src_m

    rendered = 0
    for r in results:
        code = r["code"]
        path = os.path.join(stocks_dir, f"{code}.html")
        if incremental and _fresh(code, path):
            continue                             # 데이터 안 바뀜 → 이전 페이지 재사용
        frames = frames_map[code]
        h = intraday.frame(code)                 # 시간봉 캐시(네트워크 0). 없으면 None
        if h is not None and len(h) >= h_min:
            frames = {**frames, "H": h}          # 충분하면 '시간봉' 탭 추가
        try:
            page = _detail(r, frames)
        except Exception:                        # 한 종목 실패가 전체 빌드를 막지 않도록
            page = _detail(r, {k: v for k, v in frames.items() if k != "H"})
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(page)
        rendered += 1
    if sha:
        with open(stamp, "w", encoding="utf-8") as fp:
            fp.write(sha)
    print(f"상세페이지: {rendered}/{len(results)} 렌더"
          f"{' (증분 — 나머지 재사용)' if incremental else ' (전체)'}")
    index_page, more_page = _index(results, tstats, auto)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fp:
        fp.write(index_page)
    with open(os.path.join(out_dir, "more.html"), "w", encoding="utf-8") as fp:
        fp.write(more_page)
    with open(os.path.join(out_dir, "lookup.html"), "w", encoding="utf-8") as fp:
        fp.write(_REPO and _trigger_page())   # 웹 즉석 조회(워크플로 트리거) 페이지
    with open(os.path.join(out_dir, "guide.html"), "w", encoding="utf-8") as fp:
        fp.write(_tmpl("guide.html"))          # 매매 가이드(읽기 전용 안내)
    with open(os.path.join(out_dir, "start.html"), "w", encoding="utf-8") as fp:
        fp.write(_tmpl("start.html"))          # 3분 시작 가이드(처음 사용자용)
    with open(os.path.join(out_dir, "paper.html"), "w", encoding="utf-8") as fp:
        fp.write(_paper_page(results, auto))   # 모의투자(페이퍼 트레이딩)
    os.makedirs(os.path.join(out_dir, "api"), exist_ok=True)
    with open(os.path.join(out_dir, "api", "signals.json"), "w",
              encoding="utf-8") as fp:
        fp.write(_signals_json(results))       # 자동매매용 기계 판독 시그널(JSON)
    return out_dir


def _signals_json(results: list[dict]) -> str:
    """자동매매 봇용 시그널 API — 추천을 기계가 읽는 JSON으로.

    토스/증권사 API 연동 시 실행 봇이 이 파일을 폴링해 주문 판단에 쓴다.
    id = code+날짜+그룹 → 재빌드돼도 같은 시그널은 같은 id(중복 주문 방지 멱등키).
    사람용 카드와 같은 gates 큐레이션 — 여기서 조건을 추가/변형하지 말 것.
    """
    import datetime
    import json
    from scanner import gates
    now_utc = datetime.datetime.utcnow()
    day = now_utc.strftime("%Y-%m-%d")
    sigs = []
    for r in results:
        c = gates.classify(r)
        if c["group"] is None:
            continue
        risk = r.get("risk") or {}
        sigs.append({
            "id": f'{r["code"]}-{day}-{c["group"]}',
            "code": r["code"], "name": r["name"], "ccy": r.get("ccy", "USD"),
            "group": c["group"],                       # now=지금 진입 / watch=관찰
            "entry_kind": r.get("entry_kind"),
            "stage": r.get("transition_stage", 0),
            "price": round(float((r.get("sr") or {}).get("price") or 0), 4),
            "entry": round(float(r.get("entry") or 0), 4),
            "stop": round(float(risk.get("stop") or 0), 4),
            "target": round(float(risk.get("target") or 0), 4),
            "shares_1pct": risk.get("shares", 0),      # 계좌 1% 리스크 기준 수량
            "range_pos": round(float(r.get("range_pos", 0.5)), 4),
            "norm": round(float(r.get("norm", 0)), 1),
            "fresh": _freshness(r)[0],           # 갓 전환(추세선 부근) 여부 — 우선순위↑
            "break_gap": (round(r["break_gap"], 4)
                          if r.get("break_gap") is not None else None),
            "earnings_d": earnings.days_until(r["code"]),  # 어닝까지 D-일수(없으면 null)
            # 진입 전술(즉시/반반/눌림 지정가) — 자동매매 봇이 주문 방식 결정에 사용:
            #   full=시장가 분할, half=절반 시장가+절반 pb_price 지정가, pullback=pb_price 지정가만
            "tactic": _plan_tactic(r),
        })
    sigs.sort(key=lambda s: (s["group"], not s["fresh"], -s["stage"], -s["norm"]))
    return json.dumps({
        "version": 1,
        "generated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "차트 기반 시그널 — 주문 전 가격·체결가능성 재확인 필수. 투자권유 아님.",
        "signals": sigs,
    }, ensure_ascii=False, indent=1)


# ── 모의투자(페이퍼 트레이딩) 페이지 ──────────────────────────────
# 추천 판정·품질 게이트는 전부 scanner/gates.py(단일 출처) — 여기엔 조건 없음.
from scanner.plan import freshness as _freshness   # 돌파 신선도 — plan과 공용


def _pick_item(r: dict, th: dict) -> dict:
    risk = r.get("risk") or {}
    p = (r.get("sr") or {}).get("price")
    fresh, fresh_label = _freshness(r)
    gap = r.get("break_gap")
    t = _plan_tactic(r)
    return {
        "code": r["code"], "name": r["name"], "ccy": r.get("ccy", "USD"),
        "price": round(float(p), 4) if p else 0,
        "day_chg": round(float(r.get("day_chg", 0)) * 100, 2),   # 전일 대비 %
        "sig": r["gauge"], "stage": r.get("transition_label") or "",
        "verdict": th["verdict"], "thesis": th["thesis"],
        "entry": round(float(r.get("entry") or 0), 4),
        "stop": round(float((risk.get("stop") or 0)), 4),
        "target": round(float((risk.get("target") or 0)), 4),
        "range_pos": round(float(r.get("range_pos", 0.5)) * 100),  # 52주 범위 내 %
        "fresh": fresh, "freshness": fresh_label,
        "break_gap": round(gap * 100, 1) if gap is not None else None,
        "tactic": t,                     # 진입 전술(즉시/반반/눌림 지정가) — 없으면 None
        "rec": _rec_n(r),                # 체크리스트 점수(0~6) — 매수 사유 기록용
        "stage_n": r.get("transition_stage", 0),
        "earnings_d": earnings.days_until(r["code"]),  # 어닝까지 D-일(캐시만, 없으면 None)
    }


def _paper_picks(results: list[dict]) -> dict:
    """추천 큐레이션: '지금 진입'(now) vs '곧 올 자리'(watch).

    판정은 전부 gates.classify(단일 출처)에 위임 — 여기서 조건을 추가하지 말 것.
    (과거 이 함수 안의 OR-분기 인라인 게이트가 우회 구멍의 원인이었음: NXPI·KRC)
    """
    import config
    from scanner import gates
    now, watch = [], []
    for r in results:
        c = gates.classify(r)
        if c["group"] is None:
            continue
        item = _pick_item(r, _plan_thesis(r))
        # 정렬 1순위 = 돌파 신선도(사용자 원칙: 갓 깨고 도는 후보 우선,
        #   깨고 한참 올라 횡보는 승률↓ → 뒤로)
        if c["group"] == "now":
            now.append((item["fresh"], _rec_n(r), r.get("norm", 0), item))
        else:
            watch.append((item["fresh"], r.get("transition_stage", 0),
                          r.get("norm", 0), item))
    now.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    watch.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)

    def _dedup(rows, seen, n):
        out = []
        for row in rows:
            i = row[-1]
            if i["name"] in seen:
                continue
            seen.add(i["name"])
            out.append(i)
            if len(out) >= n:
                break
        return out

    seen = set()
    now_p = _dedup(now, seen, config.PICKS_MAX)
    watch_p = _dedup(watch, seen, config.PICKS_MAX)
    return {"now": now_p, "watch": watch_p}


def _paper_page(results: list[dict], auto: dict | None = None) -> str:
    import json
    prices = {}
    for r in results:
        p = (r.get("sr") or {}).get("price")
        if p is None:
            continue
        prices[r["code"]] = [r["name"], round(float(p), 4), r.get("ccy", "USD")]
    picks = _paper_picks(results)
    return (_tmpl("paper.html")
            .replace("__PRICES__", json.dumps(prices, ensure_ascii=False))
            .replace("__PICKS__", json.dumps(picks, ensure_ascii=False))
            .replace("__AUTOP__", json.dumps(auto or {}, ensure_ascii=False))
            .replace("__TABBAR__", _tabbar("paper"))
            .replace("__FX__", "1380"))


# 저장소 정보(워크플로 트리거 대상). 다른 저장소면 여기만 바꾸면 됨.
_REPO = "easyseop/Stock-chart-analyze"


def _trigger_page() -> str:
    return _tmpl("lookup.html").replace("__REPO__", _REPO)








