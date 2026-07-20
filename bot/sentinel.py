"""매도 전용 파수꾼(sell-only stop sentinel) — 실거래 손절 보호용 상시 프로세스.

왜 존재하나 (SRE 검토 §3 채택):
  GitHub Actions 15분 배치는 best-effort라(실측: 크론 9시간 드랍) 실계좌
  손절을 맡길 수 없다. 이 프로세스는 사용자 PC/라즈베리파이에서 상시 돌며
  '보유 포지션의 손절 집행'만 담당한다. 신호 생성·신규 진입은 계속 GitHub.

설계 원칙(보안·안전):
  ① 매도 전용 — 이 코드에는 매수 경로 자체가 없다. 토큰이 유출돼도 이 모듈로
     살 수 있는 것은 없다.
  ② 기본 dry-run — SENTINEL_LIVE=1 을 명시해야만 실제 주문. 그 전엔 판단만
     로그·텔레그램으로 보고.
  ③ 시크릿은 환경변수만 — 코드·저장소에 키를 넣지 않는다.
     (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID/TOSS_APP_KEY/TOSS_APP_SECRET)
  ④ 멱등 주문 — 종목당 주문키(idempotency key)로 재시작·중복 폴링에도
     같은 손절이 두 번 나가지 않는다(로컬 sent 기록).
  ⑤ 신선도 가드 — 포지션 정보(state feed)가 30분 이상 낡으면 '신규 판단'을
     멈추고 이미 알던 손절선만 지킨다. 피드가 죽었다고 보호까지 멈추지 않는다.
  ⑥ 장중만 폴링 — 장외엔 잠들어 API/시세 호출 0.

손절 규칙(autopaper와 동일 철학):
  하드: 현재가 ≤ 손절가 −1% → 즉시 전량 (급락 방어)
  소프트: 현재가 ≤ 손절가 2연속 확인(폴링 2회≈40초) → 전량
  ※ 손절가는 서버(autopaper)가 래칫으로 올려둔 값을 feed에서 받는다 —
    파수꾼은 '집행'만 하고 '판단(래칫·익절)'은 서버가 한다. 규칙 단일 출처 유지.

사용:
  python -m bot.sentinel --once            # 1회 점검(테스트)
  python -m bot.sentinel                   # 상시 실행(기본 dry-run)
  SENTINEL_LIVE=1 python -m bot.sentinel   # 실제 주문 모드(토스 어댑터 구현 후)

지금 상태: 토스 주문 어댑터는 자리만 있음(_TossBroker) — API 키 발급 후
place_sell()만 채우면 즉시 실전 투입 가능. 그 전까지는 dry-run으로 검증 운전.
"""
from __future__ import annotations

import datetime
import json
import os
import time
import urllib.request

from bot import ledger

POLL_SEC = 20              # 장중 폴링 주기(초) — 손절 보호용이라 짧게
FEED_URLS = (              # 포지션/손절선 소스 — state 브랜치 우선, Pages 폴백
    "https://raw.githubusercontent.com/easyseop/Stock-chart-analyze/state/feed/autopaper.public.json",
    "https://easyseop.github.io/Stock-chart-analyze/api/paper_auto.json",
)
FEED_STALE_MIN = 30        # 피드가 이보다 낡으면 '보호 모드'(기존 손절선만 유지)
HARD_BUFFER = 0.01         # 하드 손절 = 손절가 −1% 이탈 시 즉시
SENT_PATH = os.path.join(os.path.dirname(__file__), "sentinel_sent.json")
LIVE = os.environ.get("SENTINEL_LIVE") == "1"      # 명시해야만 실주문


def _now_kst() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))


def _market_open(ccy: str) -> bool:
    """장중 판정 — bot.settings.market_open과 동일 규칙(ET·KST)."""
    from bot import settings as cfg
    return cfg.market_open(ccy)


def _notify(text: str, *, critical: bool = False) -> None:
    try:
        from bot import notify
        notify.send(text, critical=critical)
    except Exception:
        pass


def _fetch_positions() -> tuple[list[dict], float | None]:
    """보유 포지션 + 피드 나이(분). 소스 체인 순서대로 시도."""
    for url in FEED_URLS:
        try:
            with urllib.request.urlopen(url + "?cb=" + str(int(time.time())),
                                        timeout=15) as resp:
                d = json.load(resp)
            age = None
            try:                                   # 같은 경로의 하트비트로 나이 판정
                hb_url = url.rsplit("/", 1)[0] + "/heartbeat.json"
                with urllib.request.urlopen(
                        hb_url + "?cb=" + str(int(time.time())), timeout=10) as r2:
                    hb = json.load(r2)
                t = datetime.datetime.fromisoformat(hb["generated_at"])
                age = (_now_kst() - t).total_seconds() / 60
            except Exception:
                age = None                         # 나이 미상 — 보수적으로 신선 취급
            return d.get("positions", []), age
        except Exception:
            continue
    return [], None


class _PaperBroker:
    """dry-run 브로커 — 판단을 실행하지 않고 보고만 한다(기본값)."""
    name = "paper(dry-run)"

    def quote(self, code: str, ccy: str) -> float | None:
        # 1순위: 토스 시세(키 있을 때만·실패 시 조용히 폴백) — 손절 감시용 최신가.
        try:
            from bot import toss
            px = toss.quote_price(code)
            if px is not None:
                return px
        except Exception:
            pass
        try:
            import FinanceDataReader as fdr
            df = fdr.DataReader(code)
            return float(df["Close"].iloc[-1])
        except Exception:
            return None

    def place_sell(self, code: str, qty: int, reason: str, key: str) -> bool:
        print(f"  [DRY-RUN] 매도 {code} {qty}주 — {reason} (key={key})")
        return True


class _KisBroker(_PaperBroker):
    """KIS 어댑터(X4) — 매도 전용. 주문은 kis_orders.place_sell만 사용(매수 없음).

    안전:
      · kis_orders가 자체 게이트(모의 전용 하드블록·KIS_ORDERS_ENABLED·원장
        can_submit)를 다시 검사 — 파수꾼이 실수로 불러도 live 전송 불가.
      · 손절가는 연속장 시장가가 없어(KIS 미국주) **마켓터블 지정가**로 발주.
      · 응답은 _norm_result 규약(dict{state, filled})으로 정규화해 원장과 일치.
    """
    name = "kis"

    def __init__(self):
        from bot import kis
        if not kis.enabled():
            raise SystemExit("KIS appkey/appsecret 환경변수 필요")
        self.env = kis.ENV

    def quote(self, code: str, ccy: str) -> float | None:
        """시세 — **KIS 현재가 우선**(서버에 FDR/toss 없어도 손절 판단 가능).
        실패 시 부모(_PaperBroker: toss/FDR)로 폴백."""
        from bot import kis
        market = kis.market_of_symbol(code)
        px = kis.last_price(code, market=market)
        return px if px is not None else super().quote(code, ccy)

    def place_sell(self, code: str, qty: int, reason: str, key: str):
        from bot import kis, kis_orders
        market = kis.market_of_symbol(code)      # 국내 6자리 숫자=KR, 그 외=US
        px = self.quote(code, "KRW" if market == "KR" else "USD")
        if px is None:                       # 시세 없이 손절가 산출 불가 — 보류
            return {"state": "rejected", "filled": 0}
        if market == "KR":
            # 국내 손절은 **시장가**(체결 보장 — 사용자 정책 2026-07-13). 국내는
            #   미국과 달리 연속장 시장가가 있어 미체결 방치 위험을 없앤다.
            r = kis_orders.place_sell(key, code, qty, px, reason=reason,
                                      market=market, order_type="market")
        else:
            # 미국주는 시장가 부재 → 마켓터블 지정가(급락 시 chase가 보완).
            #   거래소는 실제 상장처로 해석(감사 수정 #1: NASD 하드코딩 시 NYSE/AMEX
            #   손절 주문이 live에서 거부돼 무한 재발화·손절 실패).
            limit = kis_orders.marketable_limit_price(px, "SELL", market=market)
            r = kis_orders.place_sell(key, code, qty, limit, reason=reason,
                                      market=market, excg=kis.us_excg_of(code))
        act = r.get("act")
        if act == "ack":                     # 접수됨(in-flight) — 체결은 대사가 확정
            return {"state": "ack", "filled": 0}
        if act == "unknown":                 # 응답 유실 — 원장이 이미 잠금
            return {"state": "unknown", "filled": 0}
        # blocked/reject/rate_limited — 명시 실패(체결 없음)
        return {"state": "rejected", "filled": 0}

    def reconcile_unknowns(self):
        """UNKNOWN 대사 — nccs+ccnl 기반(kis_boot 재사용). 반환: 대사 결과 목록."""
        from bot import kis_boot
        return kis_boot.boot_reconcile().get("results", [])

    def holdings(self):
        """KIS 실보유 {symbol.upper(): qty} — 브로커-진실 보호용. **현재 열린 시장만**
        조회(닫힌 시장은 어차피 못 팜). 하나라도 조회 실패=None(파수꾼이 feed 폴백)."""
        from bot import kis, settings
        merged: dict = {}
        plan = []
        if settings.market_open("KRW"):
            plan.append(("KR", "NASD"))
        if settings.market_open("USD"):
            plan += [("US", "NASD"), ("US", "NYSE"), ("US", "AMEX")]
        for market, ex in plan:
            h = kis.holdings(market, excg=ex)
            if h is None:
                return None
            merged.update(h)
        return merged


class _TossBroker(_PaperBroker):
    """토스증권 어댑터 자리 — API 키 발급 후 이 두 메서드만 구현하면 실전.

    보안: 키는 환경변수(TOSS_CLIENT_ID/TOSS_CLIENT_SECRET)로만. 이 클래스에도
    매수 메서드는 두지 않는다(매도 전용 원칙).
    """
    name = "toss"

    def __init__(self):
        self.key = os.environ.get("TOSS_CLIENT_ID")
        self.secret = os.environ.get("TOSS_CLIENT_SECRET")
        if not (self.key and self.secret):
            raise SystemExit("TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 환경변수 필요")

    def quote(self, code: str, ccy: str) -> float | None:
        # 시세는 Stage 0 어댑터(읽기 전용) 재사용 — 실패 시 부모 폴백.
        from bot import toss
        px = toss.quote_price(code)
        return px if px is not None else super().quote(code, ccy)

    def place_sell(self, code: str, qty: int, reason: str, key: str) -> bool:
        raise NotImplementedError("토스 매도 주문 API 연동 지점(멱등키 포함)")


def _load_sent() -> dict:
    try:
        with open(SENT_PATH, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {}


def _save_sent(d: dict) -> None:
    with open(SENT_PATH, "w", encoding="utf-8") as fp:
        json.dump(d, fp, ensure_ascii=False, indent=1)


def _norm_result(res, qty: int) -> tuple[str, int]:
    """브로커 place_sell 응답 정규화 → (state, filled_qty).
    레거시 bool(True=체결/False=거부)과 리치 dict({state, filled}) 모두 지원.
    타임아웃은 브로커가 dict{state:"unknown"}로 신호(초과매도 방지의 진입점)."""
    if isinstance(res, dict):
        return res.get("state", "filled"), int(res.get("filled", qty))
    return ("filled", qty) if res else ("rejected", 0)


def _reconcile_open(broker) -> None:
    """미해소 UNKNOWN 주문을 브로커 실측 체결량으로 확정(대사) — 잠금 해제.
    KIS 어댑터는 nccs+ccnl 대사(reconcile_unknowns), 그 외는 order_status 지원 시.
    모의/dry-run은 no-op."""
    if hasattr(broker, "reconcile_unknowns"):      # KIS 경로(대사 채널 A+B)
        try:
            for r in broker.reconcile_unknowns():
                if r.get("confidence") == ledger.CONF_HIGH:
                    _notify(f"🔁 파수꾼 대사 — {r.get('symbol')} {r.get('state')}"
                            f"(체결 {r.get('filled')} · 잔여 {r.get('residual')})",
                            critical=True)
        except Exception as e:
            print(f"[대사 오류] {type(e).__name__}: {e}")
        return
    if not hasattr(broker, "order_status"):
        return
    for o in ledger.open_orders():
        if o.get("state") != "unknown":
            continue
        try:
            actual = broker.order_status(o["key"])   # 실제 체결 수량(없으면 None)
        except Exception:
            continue
        if actual is not None:
            r = ledger.reconcile(o["key"], int(actual))
            _notify(f"🔁 파수꾼 대사 — {o.get('symbol')} 주문 확정 {r['state']}"
                    f"(체결 {r['filled']} · 잔여 {r['residual']})", critical=True)


def check_once(broker, state: dict) -> None:
    """한 사이클: 피드 → 장중 보유 종목 시세 → 하드/소프트 손절 판단."""
    _reconcile_open(broker)                    # 먼저 UNKNOWN 대사(잠금 해제 기회)
    positions, age = _fetch_positions()
    stale = age is not None and age > FEED_STALE_MIN
    if stale and not state.get("_stale_warned"):
        state["_stale_warned"] = True
        _notify(f"⚠️ 파수꾼: 포지션 피드 {age:.0f}분 낡음 — 알고 있던 "
                f"손절선으로 보호 계속(신규 판단은 보류)", critical=True)
    if not stale:
        state["_stale_warned"] = False
        state["positions"] = {p["code"]: p for p in positions}  # 최신 스냅샷 유지
    feed = state.get("positions", {})

    # 브로커-진실: KIS 실보유를 보호 대상으로(수량=브로커 진실). 손절선은 feed(트레일링)
    #   우선, 없으면 매수 루프가 기록한 진입 손절선(kis_positions). feed에도 기록에도
    #   손절선이 없는 KIS 보유는 무보호 → P0(새로 무보호가 된 것만, 알림 폭주 방지).
    #   브로커 조회 실패=None → feed 폴백(기존 동작 유지, 보호 끊기지 않게).
    held = feed
    if hasattr(broker, "holdings"):
        bh = broker.holdings()
        if bh is not None:
            from bot import kis_positions
            kpos = kis_positions.load()
            held, unprot = {}, set()
            for code, qty in bh.items():
                if int(qty) <= 0:
                    continue
                # 손절선 있는 소스 선택 — feed(트레일링) 우선, 없으면 진입 기록.
                fsrc, ksrc = feed.get(code), kpos.get(code)
                src = (fsrc if (fsrc and fsrc.get("stop"))
                       else ksrc if (ksrc and ksrc.get("stop")) else None)
                if src is None:
                    unprot.add(code)
                    continue
                held[code] = {**src, "code": code, "q": int(qty), "_bt": True}
            for code in unprot - state.get("_unprot", set()):     # 새 무보호만 알림
                _notify(f"🚨 손절선 불명 KIS 보유 {code} — 수동 확인 필요(브로커-진실)",
                        critical=True)
            state["_unprot"] = unprot
    sent = _load_sent()
    for code, p in held.items():
        if not _market_open(p.get("ccy", "USD")):
            continue
        # 미해소 in-flight 주문(submitted/ack/unknown) 있으면 재발주 전면 금지 —
        #   (대사 전 재발주 = 초과매도. is_locked는 unknown만 봐 wedged ack/submitted를
        #    놓치므로 open_order_count로 넓게 막는다 — 검증 지적 반영.)
        if ledger.open_order_count(code) >= 1:
            continue
        stop = p.get("stop")
        qty = p.get("q", 0)
        if not stop or qty <= 0:
            continue
        # 멱등키: 종목+손절가+날짜 — 같은 손절이 두 번 나가지 않게(dry-run엔 충분).
        # ── 실거래 전 필수(토스 어댑터 구현 시 반드시 강화) ─────────────
        #   [키] SRE 재검토(07-07)의 4개 구멍(부분체결 재시도 차단·트레일 stop 변경
        #   중복·당일 재진입 충돌·KR/US date 혼재) → 포지션 정체성 키로 교체:
        #        {broker}:{account}:{symbol}:{opened_at}:{seq}:{action}:{stop_version}
        #   (stop_version: 트레일 변경마다 +1 — 2차 검토(07-08) 추가)
        #   [원장] submitted→ack→partial→filled / rejected / unknown→reconcile.
        #   timeout은 실패가 아니라 UNKNOWN — 즉시 재주문 금지, 해당 종목 lock,
        #   open orders+positions 대사 후 '잔여 수량만' 새 키로. (초과매도 방지)
        #   [브로커가 진실] 보유·평단·주문가능수량은 매번 브로커 API 재조회 —
        #   state feed는 손절선 '참고'용. 수량 불일치(액면분할·기업행위 포함)면
        #   신규 주문 금지 + 수동 리뷰 경보.
        #   [매도 허용 조건] broker 실보유>0 ∧ feed에 open position ∧ 정체성 일치
        #   ∧ quote 나이 ≤10~30초(낡은 시세로 손절 판단 금지) ∧ stop 래칫(절대
        #   하향 금지 — 파수꾼 내부에서도 검증: feed가 낡아도 마지막 확정 stop은
        #   계속 집행) ∧ intended_qty ≤ broker_available.
        #   [preflight/kill switch] 장 시작 전 브로커 인증·주문권한 확인(실패=P0,
        #   warning 아님) · 하루 매도 N회 초과/동일 종목 UNKNOWN 2회/브로커-로컬
        #   불일치 → 신규 주문 전면 금지 + P0.
        #   [0차 방어] 브로커 서버측 stop 지원 시 매수 직후 등록 — 파수꾼·GitHub·
        #   CF가 전부 죽어도 남는 최후 방어선(RELIABILITY_PLAN B0).
        #
        # [현 단계 강화] 멱등키를 '포지션 정체성'으로 교체(감사 rank9):
        #   {broker}:{account}:{symbol}:{opened}:sell
        #   · raw stop 제거 → 트레일로 손절선이 바뀌어도 같은 포지션엔 재발화 없음
        #     (기존 {code}:{stop}:{date}는 stop 변경 시 새 키가 되어 중복 발화 위험).
        #   · 날짜 대신 포지션 opened → KR/US 날짜 혼재·해 넘김 문제 제거, 재진입은
        #     새 opened로 구분(같은 날 재진입은 autopaper 쿨다운이 이미 차단).
        #   · seq/stop_version·부분체결 잔여 재발주는 주문 원장(rank10)에서 완성 —
        #     그때 이 키에 :{seq}:{stop_version} 추가. 지금은 포지션당 1회 발화가 정답.
        acct = os.environ.get("TOSS_ACCOUNT", "default")
        opened = p.get("opened") or "?"
        key = f'{broker.name}:{acct}:{code}:{opened}:sell'
        if key in sent:
            continue
        px = broker.quote(code, p.get("ccy", "USD"))
        if px is None:
            continue
        hard = stop * (1 - HARD_BUFFER)
        fire, reason = False, ""
        if px <= hard:
            fire, reason = True, f"하드 손절(손절가 −{HARD_BUFFER*100:.0f}% 이탈)"
        elif px <= stop:
            if state.get("_hit_" + code):          # 2연속 확인(소프트)
                fire, reason = True, "소프트 손절(2연속 확인)"
            else:
                state["_hit_" + code] = True
        else:
            state["_hit_" + code] = False
        if fire:
            # 발주 수량: 브로커-진실(_bt)이면 실보유 전량(broker_q가 이미 잔여를 반영 —
            #   filled_for를 또 빼면 이중차감). feed 경로는 원수량−확인체결(잔여만).
            #   (부분체결 후 원수량 재발주 = 초과매도. 시도마다 별도 주문키.)
            qty_send = int(qty) if p.get("_bt") else (qty - ledger.filled_for(key))
            if qty_send <= 0:
                sent[key] = _now_kst().isoformat(timespec="seconds")   # 이미 다 팔림
                _save_sent(sent)
                continue
            okey = f"{key}#{ledger.attempts(key) + 1}"       # 이번 주문 시도의 고유키
            # 국내 잔고대사 기준: 이 주문 직전 보유수량(=잔여 qty_send, 파수꾼이 이미
            #   아는 값이라 손절 핫패스에 API 호출 추가 없음) + 방향·시장 명시 기록.
            from bot import kis as _kis
            ledger.record_submit(okey, code, qty_send, reason,   # send 이전 기록(크래시 대비)
                                 meta={"side": "SELL",
                                       "market": _kis.market_of_symbol(code),
                                       "hldg_before": int(qty_send)})
            res = broker.place_sell(code, qty_send, reason, okey)
            rstate, filled = _norm_result(res, qty_send)
            ledger.on_result(okey, rstate, filled)
            if rstate == "filled" or (rstate == "partial"
                                      and ledger.filled_for(key) >= qty):
                sent[key] = _now_kst().isoformat(timespec="seconds")
                _save_sent(sent)
                try:                               # 전량 매도 확정 → 진입 기록 정리
                    from bot import kis_positions
                    kis_positions.close(code)
                except Exception:
                    pass
            if rstate in ("filled", "partial"):
                _notify(f"🛡️ 파수꾼 매도 — {p.get('name', code)}({code}) "
                        f"{filled}주 @ {px} · {reason}"
                        + ("" if LIVE else " [DRY-RUN]"), critical=True)
            elif rstate == "ack":
                # KIS 정상 경로 — 주문응답은 '접수'까지. 체결 확정·잔여 판단은
                #   잔고대사(_resolve_acks)가 하고 '✅ 체결 확정' 알림이 뒤따른다.
                _notify(f"🛡️ 파수꾼 매도 접수 — {p.get('name', code)}({code}) "
                        f"{qty_send}주 @ {px} · {reason} (체결은 잔고대사로 확정)"
                        + ("" if LIVE else " [DRY-RUN]"), critical=True)
            elif rstate == "unknown":
                # 응답 불명 — 종목 잠금(원장). 대사 전엔 재발주 금지(초과매도 방지).
                _notify(f"⚠️ 파수꾼 주문 응답 불명(UNKNOWN) — {code} 대사까지 잠금. "
                        f"수동 확인 권장.", critical=True)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="매도 전용 손절 파수꾼(기본 dry-run)")
    ap.add_argument("--once", action="store_true", help="1회 점검 후 종료")
    args = ap.parse_args()
    want = os.environ.get("SENTINEL_BROKER", "").lower()
    if want == "kis":                              # KIS(모의) 어댑터 — X4
        broker = _KisBroker()
    elif LIVE and os.environ.get("TOSS_APP_KEY"):
        broker = _TossBroker()
    else:
        broker = _PaperBroker()
    print(f"파수꾼 시작 — 브로커={broker.name} · 폴링 {POLL_SEC}초 · "
          f"{'⚠️ 실주문 모드' if LIVE else 'dry-run(기본)'}")
    if LIVE and broker.name not in ("toss", "kis"):
        raise SystemExit("SENTINEL_LIVE=1인데 브로커 키 없음 — 안전을 위해 종료")
    if broker.name == "kis":                       # 부팅 대사(O4) — 재시작 안전
        from bot import kis_boot
        try:                                       # 대사 실패가 파수꾼 시작을 막지 않게
            s = kis_boot.boot_reconcile()          #   (fail-closed: 게이트는 닫힌 채 유지)
            print(f"부팅 대사: unknowns={s['unknowns']} resolved={s['resolved']} "
                  f"low={s['low']} ok={s['ok']}")
        except Exception as e:
            print(f"[부팅 대사 오류] {type(e).__name__}: {e} — 게이트 닫힌 채 진행")
    state: dict = {}
    while True:
        try:
            check_once(broker, state)
        except Exception as e:                     # 파수꾼은 죽지 않는다
            print(f"[오류] {type(e).__name__}: {e}")
        try:                                       # 생존성 SLA(R4) — 매 사이클 기록
            from bot import heartbeat
            heartbeat.write({"broker": broker.name,
                             "positions": len(state.get("positions", {}))})
        except Exception:
            pass
        if args.once:
            break
        # 장외엔 60초 간격으로만 깨어나 확인(호출 0)
        any_open = _market_open("USD") or _market_open("KRW")
        time.sleep(POLL_SEC if any_open else 60)


if __name__ == "__main__":
    main()
