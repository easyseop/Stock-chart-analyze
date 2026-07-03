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
                          thesis as _plan_thesis)


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
            f'title="내 종목(매수) 담기">☆</button>'
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


def _rec_card(p: dict, kind: str) -> str:
    """홈 추천 카드 한 장(모의투자 큐레이션과 동일 — 진입 논리 포함)."""
    ccy = p["ccy"]
    fp = (lambda v: f"{v:,.0f}원") if ccy == "KRW" else (lambda v: f"${v:,.2f}")
    vc = "up" if "타당" in p["verdict"] else ("dn" if "회피" in p["verdict"] else "")
    stage = f'<span class="rtag">{html.escape(p["stage"])}</span>' if p["stage"] else ""
    rp = p.get("range_pos", 50)
    low_tag = (f'<span class="rtag" style="background:#eff6ff;color:#1d4ed8" '
               f'title="52주 저가~고가 범위에서 현재가 위치(낮을수록 저점)">'
               f'📍 저점권 {rp}%</span>')
    fl = p.get("freshness")
    if fl:
        style = ("background:#fff7ed;color:#c2410c" if p.get("fresh")
                 else "background:#f1f5f9;color:#64748b")
        gap = p.get("break_gap")
        gap_txt = f" +{gap}%" if (gap is not None and gap >= 0) else ""
        low_tag += (f'<span class="rtag" style="{style}" '
                    f'title="하락추세선 대비 현재가 위치 — 갓 깬 것이 신선한 전환">'
                    f'{html.escape(fl)}{gap_txt}</span>')
    return (
        f'<div class="rc {kind}">'
        f'<div class="rch"><a href="stocks/{p["code"]}.html">{html.escape(p["name"])}</a>'
        f'<span class="rtag">{html.escape(p["sig"])}</span>{stage}{low_tag}'
        f'<span class="rvd {vc}">{html.escape(p["verdict"])}</span></div>'
        f'<div class="rth">🤖 클로드라면: {p["thesis"]}</div>'
        f'<div class="rlv">진입 <b>{fp(p["entry"])}</b> · 손절 {fp(p["stop"])} · '
        f'목표 {fp(p["target"])}</div></div>')


def _recommend_html(results: list[dict], tstats: dict | None = None) -> str:
    """홈 상단 '클로드라면 살 종목' 추천 — 모의투자와 같은 큐레이션을 표 위에 노출."""
    picks = _paper_picks(results)
    now = "".join(_rec_card(p, "now") for p in picks["now"])
    watch = "".join(_rec_card(p, "watch") for p in picks["watch"])
    now_sec = (f'<div class="rsec">🟢 지금 진입 — 전환 확정·저점 <b>{len(picks["now"])}</b></div>{now}'
               if now else
               '<div class="rsec">🟢 지금 진입 — 전환 확정·저점 <b>0</b></div>'
               '<div class="rmuted">지금 바로 들어갈 전환 확정(저점) 종목 없음 — '
               '아래 곧 올 자리(전환 임박) 주시.</div>')
    watch_sec = (f'<div class="rsec" style="margin-top:12px">👀 곧 올 자리 — 전환 임박·눌림 대기 '
                 f'<b>{len(picks["watch"])}</b></div>{watch}' if watch else "")
    # 추천 성과(자동 채점) 요약 — 매매 지표의 신뢰도를 눈으로 검증
    if tstats and tstats.get("closed"):
        stat_html = (f'<div class="rstat">📊 지난 추천 성과(자동 채점): '
                     f'<b>{tstats["wins"]}승 {tstats["losses"]}패</b> · '
                     f'승률 <b>{tstats["win_rate"]}%</b> · 평균 '
                     f'<b>{tstats["avg_r"]:+.2f}R</b> '
                     f'<span class="rmuted">(목표 도달=승 · 손절 이탈=패 · '
                     f'{tstats["open"]}건 진행 중 · '
                     f'<a href="api/track.json">원자료</a>)</span></div>')
    elif tstats:
        stat_html = (f'<div class="rstat">📊 추천 성과 자동 채점 가동 중 — '
                     f'{tstats.get("open", 0)}건 추적, 첫 결과(목표/손절 도달)부터 '
                     f'승률 표시</div>')
    else:
        stat_html = ""
    return (f'<details class="reco" open>'
            f'<summary>🤖 클로드라면 살 전환 후보 — 하락→상승 저점 매수 (진입 논리)</summary>'
            f'<div class="rbody">{stat_html}{now_sec}{watch_sec}'
            f'<div class="rmuted">⚠️ 차트 기준 추천 · 투자권유 아님. 종목 눌러 상세 확인 · '
            f'<a href="paper.html" style="color:#15803d;font-weight:700">💰 모의투자로 연습</a></div>'
            f'</div></details>')


def _index(results: list[dict], tstats: dict | None = None) -> str:
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
    import config
    page = _tmpl("index.html")
    for k, v in {
        "__ROWS__": _rows(results), "__CHIPS__": chips,
        "__STAGE_CHIPS__": stage_chips, "__REGION_CHIPS__": region_chips,
        "__RECO__": _recommend_html(results, tstats),
        "__N__": len(results), "__RCOUNT__": rcount, "__RECMIN__": REC_MIN,
        "__CACHED__": cached, "__UNI__": uni, "__PCT__": pct,
        "__UPDATED__": updated,
        "__UPDATED_TS__": int(__import__("time").time()),  # 클라이언트 'N분 전' 표시용
        # P5: 갱신주기 문구는 config에서 — 크론 바꿀 때 안내문이 저절로 따라오게
        "__INTERVAL__": config.UPDATE_INTERVAL_MIN,
    }.items():
        page = page.replace(k, str(v))
    return page


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
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fp:
        fp.write(_index(results, tstats))
    with open(os.path.join(out_dir, "lookup.html"), "w", encoding="utf-8") as fp:
        fp.write(_REPO and _trigger_page())   # 웹 즉석 조회(워크플로 트리거) 페이지
    with open(os.path.join(out_dir, "guide.html"), "w", encoding="utf-8") as fp:
        fp.write(_tmpl("guide.html"))          # 매매 가이드(읽기 전용 안내)
    with open(os.path.join(out_dir, "start.html"), "w", encoding="utf-8") as fp:
        fp.write(_tmpl("start.html"))          # 3분 시작 가이드(처음 사용자용)
    with open(os.path.join(out_dir, "paper.html"), "w", encoding="utf-8") as fp:
        fp.write(_paper_page(results))         # 모의투자(페이퍼 트레이딩)
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
    return {
        "code": r["code"], "name": r["name"], "ccy": r.get("ccy", "USD"),
        "price": round(float(p), 4) if p else 0,
        "sig": r["gauge"], "stage": r.get("transition_label") or "",
        "verdict": th["verdict"], "thesis": th["thesis"],
        "entry": round(float(r.get("entry") or 0), 4),
        "stop": round(float((risk.get("stop") or 0)), 4),
        "target": round(float((risk.get("target") or 0)), 4),
        "range_pos": round(float(r.get("range_pos", 0.5)) * 100),  # 52주 범위 내 %
        "fresh": fresh, "freshness": fresh_label,
        "break_gap": round(gap * 100, 1) if gap is not None else None,
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


def _paper_page(results: list[dict]) -> str:
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
            .replace("__FX__", "1380"))


# 저장소 정보(워크플로 트리거 대상). 다른 저장소면 여기만 바꾸면 됨.
_REPO = "easyseop/Stock-chart-analyze"


def _trigger_page() -> str:
    return _tmpl("lookup.html").replace("__REPO__", _REPO)








