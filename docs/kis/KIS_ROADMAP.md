# KIS 실매매까지 남은 것 — 전수 목록 (2026-07-11)

> ✅=완료 · 🔨=코드 남음 · 🧪=실측(장/키 필요) · 👤=사용자 작업.
> 순서는 대략 의존 순. "가장 위험 3개"(리뷰 Q6)는 ★.

## ✅ 이미 완료 (참고)
- 문서: 준비도(에러표 포함)·전환 재계획·모의vs실전·리뷰 반영 오버라이드·핸드오프
- `bot/kis.py` 읽기 어댑터(토큰 flock·TR 테이블·classify_error·잔고/nccs/ccnl)
- `bot/ledger.py` O1 확장(ODNO·합성키·confidence·can_submit)
- `bot/kis_reconcile.py` O3 UNKNOWN 대사 · `bot/kis_ratelimit.py` 유량
- `bot/kis_orders.py` O2 주문 primitive(모의 전용·다중 게이트)
- 테스트 5종 + 장애주입 통합 · 프로브 2종(읽기·왕복)

---

## A. Stage 1.5 — 모의 주문·대사 (거의 끝, 코드 2 + 실측 2)

- 🔨 **O4 부팅 대사 루프** — 서버/프로세스 재시작 시 원장의 미해소 UNKNOWN을
  자동으로 nccs+ccnl 조회해 대사(HIGH 해제/LOW 잠금유지). 대사 완료 전 신규 진입 금지.
- 🔨 **X4 파수꾼 실매도 배선** ★ — `sentinel`이 손절 트리거 시 `kis_orders.place_sell`
  호출(dry-run 병행 유지). 마켓터블 지정가·부분체결 잔여 재주문(새 키)·UNKNOWN 잠금 연동.
- 🧪 **모의 왕복 실측**(월요일 밤 KST 22:30~) — `kis_mock_roundtrip.py`로 매수 접수→
  nccs 확인→취소→ccnl 확인. 실측 확정: 모의 주문 TR·nccs 표시·취소 계약·MGCO 에코.
- 🧪 **모의 체결 관찰**(`--fill`) — 마켓터블 매수 1주 체결→매도 청산. 체결 시뮬 품질 관찰.

## B. Stage 2 전 — 실전 준비 (코드·인프라·실측)

### B-1. 손절 신뢰성 (KIS 최대 약점 보강) ★
- 🔨 **마켓터블 지정가 chase 로직**(R5) — `max_chase_count`·`max_slippage_bps`·
  `max_time_to_exit`. 미체결 시 정정(정정 UNKNOWN이면 종목 manual lock). 초과 시 P0+런북.
- 🔨 **파수꾼 생존성 SLA**(R4) — heartbeat ≤30초/>60초 P0/>120초+보유 시 신규진입
  hard-disable. 부팅 대사 완료 전 신규 금지. 동일 서버 watchdog 프로세스.
- 🧪 **생존성 훈련** — 장중 서버 재부팅·네트워크 단절 drill(파수꾼 복구 확인).

### B-2. 상시 서버 (지금 없음)
- 🔨/👤 **상시 서버 구축** — Oracle Always-Free VM 등. systemd 단일 프로세스(루프
  A 시세·전략 / B 주문 / C 파수꾼). KIS 개인계좌는 IP allowlist 불필요라 고정 IP 부담 없음.
- 🔨 **단일 token_manager 상시화** — flock 캐시를 상시 프로세스 구성에 연결(멀티프로세스면).
- 🔨 **CF Worker dead-man ↔ 서버 하트비트 연결**(대부분 있음, 서버 붙이기만).
- 🔨 **장전 preflight 캐너리**(I5) — 토큰·계좌·잔고·nccs·ccnl 읽기 + 권한 확인(주문 없이).

### B-3. 매수 실행·사이징 (실주문 진입 로직)
- 🔨 **매수 실행기 X1** — 신호 feed → 게이트 → `place_buy`. (파수꾼과 분리, 매수는 서버 루프B.)
- 🔨 **사이징 X2 + 버그 2개 수정**(07 IS4) ★ — 분모를 계좌 equity→**고정 SEED**,
  **총량 게이트**(신규 원가 ≤ deployable). buying-power는 하향 클램프로만.
- 🔨 **시드 봉투 IS3** — `bot/envelope.py`: SEED + 원장 회계(cost_krw·proceeds). 재시작 재구성.
- 🔨 **매수여력 조회** — 실전 `inquire-psamount`(모의 미지원→잔고 계산 분기).
- 🧪 **통합증거금 결정** — cash-only(미수/신용 아님) 실측 확인 or 사전환전 정책 확정(기본 OFF).

### B-4. 진입 게이트·안전
- 🔨 **진입 게이트 I1** — 종목 상태(상장폐지·거래정지)·세션(정규장만, dayMarket off)·기업행위.
- 🔨 **kill-switch L2~4**(I6) — KIS판 재정의(파수꾼 보호는 최후까지 유지). latch+operator ack.
- 🔨 **환경분리 플래그 완비**(I4) — `ALLOW_BUY`·`ALLOW_SELL`·`LIVE_CANO`·`MAX_LIVE_RISK_PCT`.
- 🔨 **계좌 격리 IS2/IS5** — 심볼 비중첩(별도계좌면 완화)·비대칭 대사·동결.

### B-5. 계좌·키 (사용자 작업)
- 👤 **봇 전용 실전 계좌 개설 + 전용 appkey**(IS1-A 물리격리) — 기존 자산 0 확인.
- 👤/🔨 **깃·CF에서 live appsecret 제거 확인**(I4 No-Go) — 주문 키는 상시 서버에만.

## C. Stage 2 — 실주문 (점진)
- 🔨 **단계적 롤아웃 가드**(I7) — 코드 강제: 첫 주 US 정규장·whole-share·1종목·하루1건·
  risk 0.1%·ALLOWED_SYMBOLS. 이후 0.25%→1%.
- 🧪 **실전 소액 실측 트랙** — 모의 미지원분(시장가 손절·매수여력·주간거래) 첫 검증.
- 🔨 **정산 회계** — 수수료·세금·환율 스프레드를 원장 원가축에 반영(손익 정확화).

## D. 선택/나중 (Stage 2 후반~3)
- 🔨 WS 체결통보(`H0GSCNI0/9`) — 대사 1차 채널(폴링 부하↓). source of truth 아님.
- 🔨 주간거래(dayMarket) 지원 · 기업행위 자동감지 · 조건주문(국내만).

---

## 🧪 실측·대조필요 (별도 축 — 장/키 있을 때 수확)
모의 왕복 TR/nccs/ccnl 표기 · MGCO_APTM_ODNO 에코 · ODNO없는 완전체결 ccnl 특정 ·
레이트리밋 단위(앱키/계좌) · 1키=1계좌 스코프 · 소수점 주문 가능여부 · 통합증거금 신용성격 ·
미국장 중 API 점검시간 · 모의 체결 시뮬 상세.

## 요약 — 지금 당장 할 수 있는 것(주말·장 마감·키 대기)
**A의 O4·X4**(코드, 주문 없이 모킹 테스트) → **B-1 chase·SLA** → **B-3 사이징 버그 2개**.
나머지(서버·계좌·실측)는 월요일 장/네 계좌 작업과 맞물림.
