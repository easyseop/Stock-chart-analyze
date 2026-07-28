"""실행 봇 설정 — 안전장치가 기본값. 실계좌 전환 전 반드시 페이퍼로 충분히 검증.

두뇌/손 분리 아키텍처:
  [두뇌] GitHub Actions 스캐너(15분) → /api/signals.json (일봉 신호 — 느려도 됨)
  [손]   이 봇(사용자 PC/서버)      → 실시간 시세로 진입/손절 방아쇠 (빨라야 함)
"""

# ── 시그널 소스 ──────────────────────────────────────────────────
# 기계용 feed(state 브랜치)를 1순위로 — Pages 장애(실측 3회)와 알림 정확도를
# 분리. Pages는 폴백. (SRE 검토 §4 채택)
SIGNALS_FEED_URL = ("https://raw.githubusercontent.com/easyseop/"
                    "Stock-chart-analyze/state/feed/signals.latest.json")
SIGNALS_URL = "https://easyseop.github.io/Stock-chart-analyze/api/signals.json"
SIGNALS_SOURCES = (SIGNALS_FEED_URL, SIGNALS_URL)   # 순서대로 시도
# 자동매매 계좌 feed — advisor가 '이미 보유한 종목'을 제안/도달 알림에서 빼려고 읽음
#   (이미 산 종목에 "지금 진입 자리!"·"돌파 발생"은 중복·혼란 — 사용자 지적).
PAPER_FEED_URL = ("https://raw.githubusercontent.com/easyseop/"
                  "Stock-chart-analyze/state/feed/autopaper.public.json")
PAPER_URL = "https://easyseop.github.io/Stock-chart-analyze/api/paper_auto.json"
PAPER_SOURCES = (PAPER_FEED_URL, PAPER_URL)
SIGNALS_STALE_MIN = 25   # 장중에 신호가 이보다 낡으면 제안 대신 '신호 낡음' 경보
                         #   (15분 주기 + raw CDN 캐시 ~5분 여유)
SITE_URL = "https://easyseop.github.io/Stock-chart-analyze"
HOLDINGS_EDIT_URL = ("https://github.com/easyseop/Stock-chart-analyze/edit/"
                     "claude/korean-text-review-o3wmsv/holdings.json")

# ── 계좌/리스크 ──────────────────────────────────────────────────
ACCOUNT_KRW = 10_000_000     # 봇 운용 자금(원). 수량 = 1회 리스크 예산 / 주당 손절폭
RISK_PER_TRADE = 0.01        # 1회 매매 손실 허용 = 계좌의 1%
POS_CAP_FRACTION = 1.0 / 3   # 종목당 최대 비중 = 계좌의 1/3(사용자 확정).
                             #   autopaper(모의)와 동일 규칙 — 실거래도 한 종목에
                             #   시드의 1/3 초과 투입 금지. 리스크·비중 중 빡빡한 쪽.
FX_USDKRW = 1380.0           # 달러 환산(달러 시그널 수량 계산용)

# ── 안전 가드(사용자와 합의한 핵심) ─────────────────────────────
ENTRY_TOLERANCE = 0.015      # 실시간가가 시그널 진입가 ±1.5% 이내일 때만 매수
import os as _os_shelf
SHELF_MAX_POS = int(_os_shelf.environ.get("SHELF_MAX_POS", "4"))


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(_os_shelf.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(low, min(high, value))


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(_os_shelf.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(low, min(high, value))


STALL_EXIT_MODE = _os_shelf.environ.get("STALL_EXIT_MODE", "off").strip().lower()
if STALL_EXIT_MODE not in ("off", "shadow", "live"):
    STALL_EXIT_MODE = "off"
STALL_TIGHTEN_DAYS = _env_int("STALL_TIGHTEN_DAYS", 15, 5, 60)
STALL_EXIT_DAYS = _env_int(
    "STALL_EXIT_DAYS", 30, STALL_TIGHTEN_DAYS + 1, 120)
STALL_NEW_HIGH_R = _env_float("STALL_NEW_HIGH_R", 0.25, 0.05, 1.0)
STALL_BASE_TRAIL_R = _env_float("STALL_BASE_TRAIL_R", 1.5, 0.5, 3.0)
STALL_TIGHT_TRAIL_R = min(
    STALL_BASE_TRAIL_R,
    _env_float("STALL_TIGHT_TRAIL_R", 1.0, 0.25, 2.5))

# 성과 대시보드 발행 토픽(ntfy) — 서버(alpha)가 5분마다 발행, 웹 perf.html이 조회.
#   퍼센트만 담아 공개 무해(금액·계좌정보 없음). 헬스 토픽과 독립(그쪽 노출 방지).
ALPHA_DASH_TOPIC = _os_shelf.environ.get("NTFY_ALPHA_TOPIC", "stock-alpha-c81f4e2b9d")
#   ↑ 매물대 슬리브(B) 동시 보유 상한. 정합성 점검(2026-07-24): 롤아웃 캡이
#     슬리브별로 각각 적용돼 A(12)+B(12)=24까지 가능하던 구멍 → B 전용 소형
#     상한으로 총노출을 A 12 + B 4 = 16으로 제한(B 예산 5M이면 4~5개가 자연 상한).
                             #   (신호 계산 시점과 주문 시점의 가격 괴리 차단)
CONFIRMED_ONLY = True        # 확정봉 모드: 전 거래일 시그널에도 있던 종목만 매수
                             #   (미확정 일봉의 가짜 돌파가 종가에 뒤집히는 것 방지)
MAX_POSITIONS = 5            # 동시 보유 최대 종목 수
DAILY_LOSS_LIMIT = 0.02      # 하루 실현손실이 계좌의 2% 넘으면 당일 신규 매수 중지
TRADE_GROUPS = ("now",)      # 매매 대상 그룹 — 'now'(지금 진입)만. watch는 관찰용.

# ── 실행 모드 ────────────────────────────────────────────────────
BROKER = "paper"             # 'paper'=모의 체결(기본) / 'toss'=토스 API(키 필요, 미구현)
POLL_SEC = 300               # --loop 모드 폴링 주기(초)

# ── 상태 파일(봇 로컬 저장 — git 추적 안 함) ────────────────────
STATE_PATH = "bot/state.json"
SEEN_PATH = "bot/seen.json"

# ── 한국시간(KST) — 날짜 스탬프/일지 기준(러너가 UTC라도 한국 시계로) ──
import datetime as _dt
KST = _dt.timezone(_dt.timedelta(hours=9))


def today_kst() -> str:
    return _dt.datetime.now(KST).date().isoformat()


def days_ago_kst(n: int) -> str:
    return (_dt.datetime.now(KST).date() - _dt.timedelta(days=n)).isoformat()


def market_open(ccy: str) -> bool:
    """지금 해당 시장이 장중인가 — 알림을 '지금 행동 가능한' 종목만 보내려는 필터.
    autopaper._market_open과 동일 규칙(둘 다 바꿀 것).
      한국주: 평일 00:00~06:30 UTC(=09:00~15:30 KST)
      미국주: 미 동부시간(ET) 평일 09:30~16:00 — 서머타임 정확 반영
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    if ccy == "KRW":
        if now.weekday() >= 5:
            return False
        hm = now.hour * 60 + now.minute
        return 0 <= hm <= 390
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
        if et.weekday() >= 5:
            return False
        hm = et.hour * 60 + et.minute
        return 570 <= hm < 960
    except Exception:                 # tzdata 없을 때만 폴백 — DST 두 체제의 **교집합**
        #   (EDT 13:30–20:00 ∩ EST 14:30–21:00 = 14:30–20:00 UTC). 합집합(810~1260)은
        #   실제 폐장 시간에도 '개장'으로 오판(감사 수정 #11). 교집합=보수적(항상 개장인
        #   구간만) — 가장자리 1시간은 놓쳐도 닫힌 때 열렸다고 오판하진 않는다.
        if now.weekday() >= 5:
            return False
        hm = now.hour * 60 + now.minute
        return 870 <= hm < 1200


# 제안(추천)형 알림 스위치 — 사용자 요청(2026-07-12): 텔레그램은 '실제 액션'
#   (자동매매 예약/매수/손절 체결 + 보유 손절선 터치)만 받는다. '[관찰] 매수 제안'과
#   '눌림/돌파 도달' 같은 추천 알림은 기본 OFF(웹 대시보드엔 계속 표시 — 정보 손실 없음).
#   다시 켜려면 환경변수 ADVISOR_SUGGEST_ALERTS=1.
import os as _os
SUGGEST_ALERTS = _os.environ.get("ADVISOR_SUGGEST_ALERTS", "0") == "1"

# 알림 폭주 방지 — 한 실행에서 보낼 최대 개수(우선순위 높은 것부터)
BUY_ALERT_MAX = 8
ARRIVAL_ALERT_MAX = 6
# 하루 총량 상한(매수+도달 제안) — 회당 상한이 깨져도 누적 폭주는 막는 서킷브레이커.
#   미국장 밤새 ~13회 실행 × 회당 최대 14 = 이론상 180+ → 40으로 캡.
DAILY_ALERT_BUDGET = 40
