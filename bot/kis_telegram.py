"""텔레그램 조회 봇 — KIS 모의계좌 보유종목/종목상세 조회(읽기 전용).

사용자 요청(2026-07-14): 텔레그램에서 보유종목을 조회하면
  · /보유          → 종목별 수익률·수익금 요약(+통화별 합계)
  · /종목 <코드>   → 그 종목의 현재가·평단가·손절예상가 상세(실시간 재조회)
  · 코드만 보내도 상세 조회.

**읽기 전용**: 이 모듈에는 주문 경로가 전혀 없다(조회 API만). 토큰이 유출돼도
이 봇으로는 매매 불가. 매수는 kis_buyloop, 매도는 sentinel만 담당(권한 분리).

보안: 응답은 **TELEGRAM_CHAT_ID로 지정된 채팅에만**. 다른 사람이 봇에게
말을 걸어도 무시(chat.id 불일치 → 조용히 버림).

손절예상가 소스는 파수꾼과 동일: feed(트레일링) 우선, 없으면 매수 루프가
기록한 진입 손절선(kis_positions). 둘 다 없으면 '미설정'.

실행:
  python -m bot.kis_telegram            # 롱폴링 루프(서버 상시 모드)
  python -m bot.kis_telegram --once "/보유"   # 1회 응답 출력(로컬 테스트)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

_API = "https://api.telegram.org/bot{token}/{method}"
# US 잔고는 거래소별 조회 → 세 시장 병합(종목이 어디 상장인지 신호에 없음).
_US_EXCGS = ("NASD", "NYSE", "AMEX")


# ── 텔레그램 저수준 ────────────────────────────────────────────────────────
def _tg(method: str, payload: dict | None = None, *, timeout: int = 40) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    url = _API.format(token=token, method=method)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ── 포맷 ──────────────────────────────────────────────────────────────────
def _m(v, ccy: str, *, signed: bool = False) -> str:
    """금액 포맷 — KRW='1,234원' / USD='$12.34'. signed면 부호 접두."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        v = 0.0
    body = f"{abs(v):,.0f}원" if ccy == "KRW" else f"${abs(v):,.2f}"
    if signed:
        return ("+" if v >= 0 else "-") + body
    return body


# ── 데이터 수집(읽기 전용) ─────────────────────────────────────────────────
def _all_positions() -> tuple[list[dict], bool, bool]:
    """전 시장 보유 상세 병합. 반환 (rows, any_ok, any_fail).

    KR 1회 + US 거래소 3회 조회. 조회 실패(None)는 any_fail, 성공은 any_ok.
    any_ok=False(전부 실패)면 호출부가 '조회 실패' 응답.
    """
    from bot import kis
    seen: set[str] = set()
    rows: list[dict] = []
    any_ok = any_fail = False
    queries = [("KR", None)] + [("US", e) for e in _US_EXCGS]
    for market, excg in queries:
        r = (kis.positions_detail(market) if market == "KR"
             else kis.positions_detail(market, excg=excg))
        if r is None:
            any_fail = True
            continue
        any_ok = True
        for p in r:
            if p["code"] in seen:
                continue
            seen.add(p["code"])
            rows.append(p)
    return rows, any_ok, any_fail


def _stop_for(code: str) -> tuple[float | None, str]:
    """손절예상가 — feed(트레일링) 우선, 없으면 진입기록. 파수꾼과 동일 소스."""
    code = str(code).upper()
    try:
        from bot import sentinel
        positions, _age = sentinel._fetch_positions()
        for p in positions:
            if str(p.get("code") or "").upper() == code and p.get("stop"):
                return float(p["stop"]), "트레일링"
    except Exception:
        pass
    try:
        from bot import kis_positions
        k = kis_positions.load().get(code)
        if k and k.get("stop"):
            return float(k["stop"]), "진입 손절선"
    except Exception:
        pass
    return None, "미설정"


def _resolve(query: str, positions: list[dict]) -> dict | None:
    """조회어→보유 종목. 코드 정확일치 우선, 다음 종목명 부분일치."""
    q = query.strip().upper()
    for p in positions:
        if p["code"] == q:
            return p
    ql = query.strip().lower()
    if ql:
        for p in positions:
            if ql in str(p["name"]).lower():
                return p
    return None


# ── 수시수집(lookup 워크플로 디스패치) ─────────────────────────────────────
# 읽기전용 원칙의 유일한 확장이지만 **고정된 단일 액션**이다: 임의 명령
#   채널이 아니라 lookup.yml(캐시 추가→스크리너 재생성→사이트 배포) 실행
#   요청 하나뿐이고, 형식 검증을 통과한 티커 문자열만 input으로 전달된다.
#   매매 서버(주문·kill·설정)에는 아무 영향이 없다.
# 토큰: GH_PAT — fine-grained PAT(이 저장소 한정, Actions RW)만 권장.
#   /etc/stock/kis.env(600)에 두고 응답·로그에 절대 노출하지 않는다.
_GH_REPO = os.environ.get("GH_REPO", "easyseop/Stock-chart-analyze")
_GH_BRANCH = os.environ.get("GH_BRANCH", "claude/happy-gauss-cwoq21")
_TICKER_RE = re.compile(r"^(\d{6}|[A-Z][A-Z.\-]{0,9})$")   # KR 6자리 | US 티커


def _dispatch_lookup(ticker: str) -> tuple[bool, str]:
    """lookup.yml workflow_dispatch 1회 호출. (성공여부, 실패사유)."""
    token = os.environ.get("GH_PAT") or ""
    if not token:
        return False, "GH_PAT 미설정"
    url = (f"https://api.github.com/repos/{_GH_REPO}"
           "/actions/workflows/lookup.yml/dispatches")
    body = json.dumps({"ref": _GH_BRANCH,
                       "inputs": {"ticker": ticker}}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "stock-telegram-bot",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status == 204:
                return True, ""
            return False, f"HTTP {r.status}"
    except Exception as e:                       # HTTPError 포함 — 토큰 비노출
        code = getattr(e, "code", None)
        return False, f"HTTP {code}" if code else type(e).__name__


def _collect_text(arg: str) -> str:
    """/수집 <티커|코드> — 즉석 수집 요청(검증→디스패치→안내)."""
    code = (arg or "").strip().upper()
    if not _TICKER_RE.match(code):
        return ("사용법: /수집 &lt;티커|6자리코드&gt;\n"
                "예) /수집 AAPL · /수집 BRK.B · /수집 005930")
    ok, why = _dispatch_lookup(code)
    if ok:
        return (f"🛰 <b>{code} 수집 시작</b>\n"
                "5~10분 뒤 차트 사이트 목록·상세에 반영됩니다.\n"
                "이미 등록된 종목이면 데이터 갱신만 합니다.")
    if why == "GH_PAT 미설정":
        return ("⚙️ 수집 트리거 토큰(GH_PAT)이 서버에 없습니다.\n"
                "/etc/stock/kis.env에 GH_PAT=&lt;fine-grained PAT"
                "(이 저장소 Actions RW)&gt; 추가 후 "
                "telegram 서비스 재시작이 필요합니다.")
    return f"⚠️ 수집 요청 실패({why}) — 잠시 후 다시 시도해 주세요."


# ── 응답 빌더 ──────────────────────────────────────────────────────────────
def _help_text() -> str:
    return ("🤖 <b>KIS 모의계좌 조회</b>\n\n"
            "/보유 — 보유 종목별 수익률·수익금\n"
            "/종목 &lt;코드&gt; — 현재가·평단가·손절예상가 상세\n"
            "  예) /종목 005930 · /종목 AAPL\n"
            "/슬리브 — 매물대(B) 슬리브 종목별 수익률\n"
            "/성과 — 지수(나스닥·코스피) 대비 성과 + 그래프\n"
            "/진단 — 서버 건강(kill·heartbeat·KIS 조회·서비스) 실측\n"
            "/수집 &lt;티커&gt; — 종목 즉석 수집(차트 사이트 등록·갱신)\n"
            "  예) /수집 AAPL · /수집 005930\n"
            "코드만 보내도 상세 조회됩니다.")


def _holdings_text() -> str:
    rows, any_ok, any_fail = _all_positions()
    if not any_ok:
        return "⚠️ 잔고 조회 실패 — 잠시 후 다시 시도(KIS 응답 없음)."
    if not rows:
        return "📊 보유 종목 없음 (KIS 모의계좌)."
    lines = ["📊 <b>보유 종목</b> (KIS 모의계좌)", ""]
    tot: dict[str, list[float]] = {}                 # ccy → [평가, 매입, 손익]
    for p in sorted(rows, key=lambda x: -float(x["pl_rt"])):
        sign = "🟢" if p["pl_amt"] >= 0 else "🔴"
        lines.append(f"{sign} {p['name']}({p['code']})  "
                     f"{float(p['pl_rt']):+.1f}%  "
                     f"{_m(p['pl_amt'], p['ccy'], signed=True)}")
        t = tot.setdefault(p["ccy"], [0.0, 0.0, 0.0])
        t[0] += float(p["eval_amt"]); t[1] += float(p["buy_amt"])
        t[2] += float(p["pl_amt"])
    lines.append("")
    for ccy, (ev, bu, pl) in tot.items():
        rt = (pl / bu * 100) if bu else 0.0
        sign = "🟢" if pl >= 0 else "🔴"
        lines.append(f"합계 평가 {_m(ev, ccy)} · 손익 {sign} "
                     f"{_m(pl, ccy, signed=True)} ({rt:+.1f}%)")
    if any_fail:
        lines.append("⚠️ 일부 시장 조회 실패 — 목록이 불완전할 수 있음.")
    lines.append("")
    # 목록 손익은 잔고 스냅샷(준실시간) 기준 — 모의는 시세가 밀릴 수 있어
    #   정확값은 종목 상세(실시간 재조회)로 안내(사용자 혼동 방지 2026-07-23).
    lines.append("※ 손익은 준실시간(잔고 스냅샷) 기준 · 정확값은 아래 상세로")
    lines.append(f"상세: <code>/종목 {rows[0]['code']}</code> 처럼 코드로 조회 (실시간)")
    return "\n".join(lines)


def _detail_text(query: str) -> str:
    from bot import kis
    rows, any_ok, _fail = _all_positions()
    if not any_ok:
        return "⚠️ 잔고 조회 실패 — 잠시 후 다시 시도(KIS 응답 없음)."
    p = _resolve(query, rows)
    if not p:
        held = ", ".join(x["code"] for x in rows) or "없음"
        return (f"'{query.strip()}' 보유 없음.\n현재 보유: {held}\n"
                f"차트 수집이 목적이면: /수집 {query.strip().upper()}")
    code, market, ccy = p["code"], p["market"], p["ccy"]
    avg, qty = float(p["avg"]), int(p["qty"])
    cur = kis.last_price(code, market=market) or float(p["cur"])   # 실시간 재조회
    eval_amt = cur * qty if cur else float(p["eval_amt"])
    buy_amt = avg * qty if avg else float(p["buy_amt"])
    pl_amt = (cur - avg) * qty if (cur and avg) else float(p["pl_amt"])
    pl_rt = (cur / avg - 1) * 100 if (cur and avg) else float(p["pl_rt"])
    stop, ssrc = _stop_for(code)
    tag = "국내" if market == "KR" else "미국"
    L = [f"🔎 <b>{p['name']}({code})</b>  [{tag}]", "",
         f"현재가   {_m(cur, ccy)}  (실시간)",
         f"평단가   {_m(avg, ccy)}"]
    if stop is not None:
        L.append(f"손절예상가 {_m(stop, ccy)}  ({ssrc})")
        if cur and stop:
            L.append(f"  └ 현재가가 손절선까지 {(cur / stop - 1) * 100:+.1f}%")
    else:
        L.append("손절예상가 미설정 ⚠️ (수동 확인 필요)")
    L += [f"수량     {qty}주", "",
          f"평가금액  {_m(eval_amt, ccy)}",
          f"매입금액  {_m(buy_amt, ccy)}"]
    sign = "🟢" if pl_amt >= 0 else "🔴"
    L.append(f"평가손익  {sign} {_m(pl_amt, ccy, signed=True)} ({pl_rt:+.1f}%)")
    return "\n".join(L)


def _perf_text() -> str:
    """/성과 — 지수 대비 성과 온디맨드 조회(알파 상태파일 + 그래프)."""
    from bot import alpha, notify
    st = alpha._load()
    if not st.get("day") and not st.get("days"):
        return ("📊 성과 데이터 아직 없음 — 다음 장중부터 5분마다 기록됩니다.\n"
                "(장 시작·1시간·마감 자동 알림도 함께)")
    L = ["📊 <b>전략 성과 vs 지수</b> (KIS 계좌 기준)", ""]
    for mkt, label, idxn in (("US", "미장", "나스닥"), ("KR", "국장", "코스피")):
        day = (st.get("day") or {}).get(mkt)
        if day and day.get("series"):
            last = day["series"][-1]
            L.append(f"{label} {day['date']} {last[0]} 기준(세션 시작 대비):")
            if last[1] is None:                # 미확정 — 숫자·색 판정 금지(P1-1)
                L.append(f"  우리 미확정 vs {idxn} {last[2]:+.2f}% "
                         "→ 판정 보류(데이터 이상 격리, 수동 확인 필요)")
            else:
                d = last[1] - last[2]
                mark = "🟢" if d >= 0 else "🔴"
                L.append(f"  우리 {last[1]:+.2f}% vs {idxn} {last[2]:+.2f}% "
                         f"→ {mark} {d:+.2f}%p")
            try:                                   # 그래프는 별도 사진으로
                url = alpha.chart_url(day["series"], idxn,
                                      f"{day['date']} {label} 추이")
                notify.send_photo(url, f"{label} 세션 추이")
            except Exception:
                pass
        cap = alpha.capture_stats(st.get("days") or [], mkt)
        if cap:
            L.append(f"  누적: {cap}")
    L.append("")
    L.append("자동 알림: 장 시작·1시간마다·장 마감 + 그래프")
    return "\n".join(L)


def _diag_text() -> str:
    """/진단 — SSH 없이 폰에서 서버 건강 확인(전부 읽기 전용, 주문 경로 없음).

    사용자 요청(2026-08-05): 'KIS 잔고 조회 실패' 경보가 왔을 때 다른 컴퓨터
    없이 현재 상태를 확인할 방법이 필요하다. 경보는 실패 스트릭당 1회 래치라
    침묵이 복구를 뜻하지 않는다 — 이 명령이 지금 시점의 실측을 보여준다.
    """
    import subprocess
    L = ["🩺 <b>서버 자가진단</b> (읽기 전용)", ""]

    # 1) kill-switch — 신규매수 허용 수준
    lv = None
    try:
        from bot import kill
        lv = kill.level()
        L.append(f"kill-switch: L{lv} "
                 + ("(정상 — 신규매수 허용)" if lv == 0 else
                    "(신규매수 중지 · 손절은 계속)" if lv == 1 else
                    "(상위 차단 — 수동 확인)"))
    except Exception as e:
        L.append(f"kill-switch: 확인 실패({type(e).__name__})")

    # 2) 파수꾼 heartbeat — 손절 감시 생존
    try:
        from bot import heartbeat
        age = heartbeat.age_s()
        L.append("파수꾼 heartbeat: "
                 + ("기록 없음 ⚠️" if age is None else
                    f"{age:.0f}초 전 {'✅' if age <= 60 else '⚠️ 지연'}"))
    except Exception as e:
        L.append(f"파수꾼 heartbeat: 확인 실패({type(e).__name__})")

    # 자동 복구 판정은 L1일 때만 표시한다. 상태 읽기만 하며 cycle을 돌리지 않는다.
    if lv == 1:
        try:
            from bot import kill_self_heal
            healing = kill_self_heal.status()
            observed_min = max(0.0, float(healing.get("observed_s") or 0)) / 60
            why = str(healing.get("why") or "-")
            why = why.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            L.append(f"자가복구: 관찰 {observed_min:.1f}분 · "
                     f"{healing.get('action') or 'unknown'} · 사유 {why}")
        except Exception as e:
            L.append(f"자가복구: 확인 실패({type(e).__name__})")

    # 3) KIS 잔고 조회 — 시장별 실측(경보 래치와 무관한 현재 시점 프로브)
    try:
        from bot import kis
        parts = []
        fails = 0
        for market, excg in [("KR", None)] + [("US", e) for e in _US_EXCGS]:
            r = (kis.positions_detail(market) if market == "KR"
                 else kis.positions_detail(market, excg=excg))
            name = market if market == "KR" else excg
            if r is None:
                parts.append(f"{name}✗")
                fails += 1
            else:
                parts.append(f"{name}({len(r)})")
        mark = "✅" if fails == 0 else ("⚠️ 일부 실패" if fails < 4 else "🚨 전부 실패")
        L.append(f"KIS 잔고 조회: {' '.join(parts)} {mark}")
        if fails:
            L.append("  └ 실패 지속 시 손절 자동매도가 차단됩니다(수량 불명 fail-closed)")
    except Exception as e:
        L.append(f"KIS 잔고 조회: 확인 실패({type(e).__name__})")
    try:
        from bot import balance_health
        health = balance_health.summary()
        L.append(f"잔고 실패(24h): {health['count']}회 · 최다 원인 "
                 f"{health['top_cause']} (프로세스 기동 후)")
    except Exception as e:
        L.append(f"잔고 실패(24h): 확인 실패({type(e).__name__})")

    # 4) 원장 — 열린 주문·UNKNOWN(주문 경로 건강)
    try:
        from bot import ledger
        fold = ledger._fold()
        open_n = sum(1 for c in fold.values() if c.get("open_order"))
        unk = sum(1 for c in fold.values() if c.get("state") == "unknown")
        L.append(f"원장: 열린 주문 {open_n} · UNKNOWN {unk} "
                 + ("✅" if unk == 0 else "🚨 UNKNOWN 수동 확인 필요"))
    except Exception as e:
        L.append(f"원장: 확인 실패({type(e).__name__})")

    # 5) ACK 대사 건강 — buyloop/sentinel이 공유 상태파일에 기록.
    try:
        from bot import kis_boot
        health = kis_boot.reconcile_health()
        last = health.get("last_success_at")
        if last is None:
            ago = "성공 기록 없음"
        else:
            ago = f"마지막 성공 {max(0, (time.time() - float(last)) / 60):.0f}분 전"
        L.append(f"대사: {ago} · 연속 실패 "
                 f"{int(health.get('failure_streak') or 0)}회")
    except Exception as e:
        L.append(f"대사: 확인 실패({type(e).__name__})")

    # 6) 신호 피드 나이(파수꾼 참고 손절선 소스)
    try:
        from bot import sentinel
        _rows, feed_age = sentinel._fetch_positions()
        L.append("포지션 피드: "
                 + ("나이 미상" if feed_age is None else
                    f"{feed_age:.0f}분 전 {'✅' if feed_age <= 60 else '⚠️ 정체'}"))
    except Exception as e:
        L.append(f"포지션 피드: 확인 실패({type(e).__name__})")

    # 7) 서비스 상태(systemctl is-active는 무권한 읽기)
    try:
        states = []
        for unit in ("sentinel", "buyloop", "watchdog", "portfolio-web"):
            try:
                out = subprocess.run(
                    ["systemctl", "is-active", unit], capture_output=True,
                    text=True, timeout=5).stdout.strip() or "?"
            except Exception:
                out = "?"
            states.append(f"{unit}={'✅' if out == 'active' else out}")
        L.append("서비스: " + " · ".join(states))
    except Exception:
        pass

    # 7) 안전 플래그(시크릿 아님 — 값 자체가 운영 상태)
    env = os.environ
    L.append(f"환경: KIS_ENV={env.get('KIS_ENV', '?')} · "
             f"STAGE={env.get('TRADE_STAGE', '?')} · "
             f"BUY={'on' if env.get('ALLOW_BUY') == '1' else 'off'} · "
             f"ORDERS={'on' if env.get('KIS_ORDERS_ENABLED') == '1' else 'off'} · "
             f"fallback={env.get('ORACLE_SIGNAL_FALLBACK_ENABLED', '0')}")
    return "\n".join(L)


def handle(text: str) -> str:
    """메시지 텍스트 → 응답 문자열(라우팅). 빈 응답이면 무시."""
    raw = (text or "").strip()
    if not raw:
        return ""
    parts = raw.lstrip("/").split()
    cmd = parts[0].lower() if parts else ""
    if cmd in ("start", "help", "도움", "도움말", "?"):
        return _help_text()
    if cmd in ("보유", "잔고", "포지션", "holdings", "positions", "h"):
        return _holdings_text()
    if cmd in ("종목", "상세", "detail", "s"):
        if len(parts) >= 2:
            return _detail_text(" ".join(parts[1:]))
        return "사용법: /종목 &lt;코드&gt;  예) /종목 005930"
    if cmd in ("슬리브", "매물대", "sleeve", "b"):
        from bot import sleeve_stats
        return sleeve_stats.report_text()
    if cmd in ("성과", "알파", "지수", "perf"):
        return _perf_text()
    if cmd in ("진단", "상태", "diag", "status"):
        return _diag_text()
    if cmd in ("수집", "추가", "collect", "add", "lookup"):
        if len(parts) >= 2:
            return _collect_text(parts[1])
        return ("사용법: /수집 &lt;티커|6자리코드&gt;  "
                "예) /수집 AAPL · /수집 005930")
    return _detail_text(raw)          # 접두어 없이 코드/이름만 → 상세


# ── 롱폴링 루프 ────────────────────────────────────────────────────────────
def _drain_backlog() -> int:
    """시작 시 미처리 백로그를 건너뛴다(재시작 시 오래된 조회 재응답 방지)."""
    try:
        ups = _tg("getUpdates", {"timeout": 0}, timeout=15).get("result", [])
        return (ups[-1]["update_id"] + 1) if ups else 0
    except Exception:
        return 0


def _reply(text: str) -> None:
    """지정 채팅으로 전송 — notify.send 재사용(TELEGRAM_CHAT_ID로 감)."""
    try:
        from bot import notify
        notify.send(text, category="query")
    except Exception as e:
        print(f"[전송 오류] {e}", flush=True)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="KIS 모의계좌 텔레그램 조회 봇(읽기전용)")
    ap.add_argument("--once", metavar="TEXT",
                    help="롱폴링 없이 1회 응답을 출력(로컬 테스트, KIS 키만 필요)")
    args = ap.parse_args()
    if args.once is not None:                          # 폴링 전이라 TG 토큰 불필요
        print(handle(args.once))
        return 0
    if not os.environ.get("TELEGRAM_BOT_TOKEN") \
            or not os.environ.get("TELEGRAM_CHAT_ID"):
        print("TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID 환경변수 필요", flush=True)
        return 2

    chat_id = str(os.environ.get("TELEGRAM_CHAT_ID"))
    offset = _drain_backlog()
    print("텔레그램 조회 봇 시작 — /보유 · /종목 <코드> (읽기전용)", flush=True)
    while True:
        # 주기 자가진단 발행(기본 10분) — SSH 없는 원격 진단 루프(읽기 전용,
        #   실패 무해). 롱폴링(30s) 사이마다 시간만 확인하므로 비용이 없다.
        try:
            from bot import ops_status
            ops_status.maybe_publish()
            ops_status.maybe_remind_kill()   # L1+ 지속 리마인드(2026-08-10)
            ops_status.maybe_alert_stuck_acks()
            from bot import trade_stats
            trade_stats.maybe_publish()      # 공개 승률 요약(금액 제외)
        except Exception:
            pass
        try:
            res = _tg("getUpdates", {"offset": offset, "timeout": 30}, timeout=40)
        except Exception as e:
            print(f"[getUpdates 오류] {type(e).__name__}: {e}", flush=True)
            time.sleep(3)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message") or {}
            if str((msg.get("chat") or {}).get("id")) != chat_id:
                continue                              # 보안: 지정 채팅만
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            try:
                reply = handle(text)
            except Exception as e:
                reply = f"오류: {type(e).__name__}: {e}"
            if reply:
                _reply(reply)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
