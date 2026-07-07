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


def _notify(text: str) -> None:
    try:
        from bot import notify
        notify.send(text)
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
        try:
            import FinanceDataReader as fdr
            df = fdr.DataReader(code)
            return float(df["Close"].iloc[-1])
        except Exception:
            return None

    def place_sell(self, code: str, qty: int, reason: str, key: str) -> bool:
        print(f"  [DRY-RUN] 매도 {code} {qty}주 — {reason} (key={key})")
        return True


class _TossBroker(_PaperBroker):
    """토스증권 어댑터 자리 — API 키 발급 후 이 두 메서드만 구현하면 실전.

    보안: 키는 환경변수(TOSS_APP_KEY/TOSS_APP_SECRET)로만. 이 클래스에도
    매수 메서드는 두지 않는다(매도 전용 원칙).
    """
    name = "toss"

    def __init__(self):
        self.key = os.environ.get("TOSS_APP_KEY")
        self.secret = os.environ.get("TOSS_APP_SECRET")
        if not (self.key and self.secret):
            raise SystemExit("TOSS_APP_KEY/TOSS_APP_SECRET 환경변수 필요")

    def quote(self, code: str, ccy: str) -> float | None:
        raise NotImplementedError("토스 시세 API 연동 지점")

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


def check_once(broker, state: dict) -> None:
    """한 사이클: 피드 → 장중 보유 종목 시세 → 하드/소프트 손절 판단."""
    positions, age = _fetch_positions()
    stale = age is not None and age > FEED_STALE_MIN
    if stale and not state.get("_stale_warned"):
        state["_stale_warned"] = True
        _notify(f"⚠️ 파수꾼: 포지션 피드 {age:.0f}분 낡음 — 알고 있던 "
                f"손절선으로 보호 계속(신규 판단은 보류)")
    if not stale:
        state["_stale_warned"] = False
        state["positions"] = {p["code"]: p for p in positions}  # 최신 스냅샷 유지
    held = state.get("positions", {})
    sent = _load_sent()
    for code, p in held.items():
        if not _market_open(p.get("ccy", "USD")):
            continue
        stop = p.get("stop")
        qty = p.get("q", 0)
        if not stop or qty <= 0:
            continue
        # 멱등키: 종목+손절가+날짜 — 같은 손절이 두 번 나가지 않게
        key = f'{code}:{stop}:{_now_kst().date().isoformat()}'
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
            ok = broker.place_sell(code, qty, reason, key)
            if ok:
                sent[key] = _now_kst().isoformat(timespec="seconds")
                _save_sent(sent)
                _notify(f"🛡️ 파수꾼 매도 — {p.get('name', code)}({code}) "
                        f"{qty}주 @ {px} · {reason}"
                        + ("" if LIVE else " [DRY-RUN]"))


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="매도 전용 손절 파수꾼(기본 dry-run)")
    ap.add_argument("--once", action="store_true", help="1회 점검 후 종료")
    args = ap.parse_args()
    broker = _TossBroker() if (LIVE and os.environ.get("TOSS_APP_KEY")) \
        else _PaperBroker()
    print(f"파수꾼 시작 — 브로커={broker.name} · 폴링 {POLL_SEC}초 · "
          f"{'⚠️ 실주문 모드' if LIVE else 'dry-run(기본)'}")
    if LIVE and broker.name != "toss":
        raise SystemExit("SENTINEL_LIVE=1인데 토스 키 없음 — 안전을 위해 종료")
    state: dict = {}
    while True:
        try:
            check_once(broker, state)
        except Exception as e:                     # 파수꾼은 죽지 않는다
            print(f"[오류] {type(e).__name__}: {e}")
        if args.once:
            break
        # 장외엔 60초 간격으로만 깨어나 확인(호출 0)
        any_open = _market_open("USD") or _market_open("KRW")
        time.sleep(POLL_SEC if any_open else 60)


if __name__ == "__main__":
    main()
