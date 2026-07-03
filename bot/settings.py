"""실행 봇 설정 — 안전장치가 기본값. 실계좌 전환 전 반드시 페이퍼로 충분히 검증.

두뇌/손 분리 아키텍처:
  [두뇌] GitHub Actions 스캐너(15분) → /api/signals.json (일봉 신호 — 느려도 됨)
  [손]   이 봇(사용자 PC/서버)      → 실시간 시세로 진입/손절 방아쇠 (빨라야 함)
"""

# ── 시그널 소스 ──────────────────────────────────────────────────
SIGNALS_URL = "https://easyseop.github.io/Stock-chart-analyze/api/signals.json"
SITE_URL = "https://easyseop.github.io/Stock-chart-analyze"
HOLDINGS_EDIT_URL = ("https://github.com/easyseop/Stock-chart-analyze/edit/"
                     "claude/korean-text-review-o3wmsv/holdings.json")

# ── 계좌/리스크 ──────────────────────────────────────────────────
ACCOUNT_KRW = 10_000_000     # 봇 운용 자금(원). 수량 = 1회 리스크 예산 / 주당 손절폭
RISK_PER_TRADE = 0.01        # 1회 매매 손실 허용 = 계좌의 1%
FX_USDKRW = 1380.0           # 달러 환산(달러 시그널 수량 계산용)

# ── 안전 가드(사용자와 합의한 핵심) ─────────────────────────────
ENTRY_TOLERANCE = 0.015      # 실시간가가 시그널 진입가 ±1.5% 이내일 때만 매수
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
