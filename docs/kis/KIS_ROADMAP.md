# KIS 실매매까지 남은 것 — 전수 목록 (2026-07-11)

> ✅=완료 · 🔨=코드 남음 · 🧪=실측(장/키 필요) · 👤=사용자 작업.
> 순서는 대략 의존 순. "가장 위험 3개"(리뷰 Q6)는 ★.

> **2026-07-13 보강 — 국내(KR) 실행 경로 추가**: 기존 배선이 미국 해외주식만
> 다뤄 국장이 실행 계층에서 누락돼 있던 것을 복구. 이제 **국장·미장 둘 다** 주문/
> 취소/대사/세션 게이트가 심볼로 자동 라우팅된다(6자리 숫자=KR). 같은 appkey로
> 두 시장 커버. TR_ID·바디 필드는 모의 왕복 실측 전까지 [대조필요](아래 §실측).
> `test_kis_domestic.py`로 라우팅·바디·세션 게이트 불변 증명(23개 모듈 green).
>   · `kis.py` KR TR표 + domestic_balance/open_orders/fills/buying_power/last_price
>   · `kis_orders.py` 국내 order-cash 경로·바디·호가단위 정렬 마켓터블 지정가
>   · `rollout.py` session_open_for(market) — KR=한국 정규장(낮), US=미 정규장(밤)
>   · `kis_buy`(원화 사이징)·`sentinel`(심볼 라우팅)·`kis_boot`(국내 대사)
>
> **재리뷰(2026-07-13) 반영/미결**:
>   · [반영] 국내 UNKNOWN 대사는 **fail-closed 명시화** — `kis_reconcile.normalize_rows`가
>     해외 필드명(ft_*)에 결합돼 국내 응답을 못 읽으므로, 자동 해소 대신 P0 잠금 유지.
>     국내 nccs/ccnl 실제 필드명 실측 후 normalize에 매핑해야 국내 자동대사 활성화.
>   · [반영] `market_of_symbol` 앞5자리 숫자 기준 — 신형우선주(6번째 문자) 오분류 수정.
>     더 견고하게는 ccy 기반 라우팅(신호/보유의 ccy) 권장.
>   · [미결·정책] 🔨 **국내 보호매도 시장가 옵션** — 국내는 진짜 시장가(`ORD_DVSN=01`)가
>     있어 손절 체결 보장에 유리(마켓터블 지정가+chase는 미국주 시장가 부재 우회책).
>     `ORD_DVSN` 파라미터화 필요. 기본값(지정가 vs 시장가)은 사용자 정책 결정.
>   · [미결·잠재] 🔨 **chase 국내화** — `_floor`/`_ladder` 호가단위 정렬·`orgno` 결속.
>     chase 미배선이라 국내 chase 붙일 때 함께.
>
> **국내 모의 왕복 1차 실측(2026-07-13 낮 한국장)**:
>   · ✅ 국내 시세 `FHKST01010100` 작동(삼성전자 255,500원).
>   · ✅ 주문 경로·TR(`VTTC0802U`)·order-cash 바디 **구조 수용 확인** — 서버가
>     업무레벨 거부(`40270000 모의투자 상/하한가 오류`)를 반환(포맷 오류 아님).
>   · 🔧 한국주 **일일 ±30% 가격제한** 발견 — '현재가×50%' 안-체결 트릭이 하한가
>     아래라 거부됨 → 왕복 스크립트를 **하한가 매수**로 수정(`kis.price_limits`).
>   · ⏭ 재실행으로 nccs 표시·취소·ccnl 표기 실측 남음.

## ✅ 이미 완료 (참고)
- 문서: 준비도(에러표 포함)·전환 재계획·모의vs실전·리뷰 반영 오버라이드·핸드오프
- `bot/kis.py` 읽기 어댑터(토큰 flock·TR 테이블·classify_error·잔고/nccs/ccnl)
- `bot/ledger.py` O1 확장(ODNO·합성키·confidence·can_submit)
- `bot/kis_reconcile.py` O3 UNKNOWN 대사 · `bot/kis_ratelimit.py` 유량
- `bot/kis_orders.py` O2 주문 primitive(모의 전용·다중 게이트)
- 테스트 5종 + 장애주입 통합 · 프로브 2종(읽기·왕복)

---

## A. Stage 1.5 — 모의 주문·대사 (**코드 완료** — 실측 2개만 남음)

- ✅ **O4 부팅 대사 루프** — `bot/kis_boot.py`(trading_allowed 게이트·fail-closed·LOW P0).
- ✅ **X4 파수꾼 실매도 배선** ★ — `sentinel._KisBroker`(마켓터블 지정가·정규화·대사 연동).
- 🧪 **모의 왕복 실측**(월요일 밤 KST 22:30~) — `kis_mock_roundtrip.py`로 매수 접수→
  nccs 확인→취소→ccnl 확인. 실측 확정: 모의 주문 TR·nccs 표시·취소 계약·MGCO 에코.
- 🧪 **모의 체결 관찰**(`--fill`) — 마켓터블 매수 1주 체결→매도 청산. 체결 시뮬 품질 관찰.

## B. Stage 2 전 — 실전 준비 (코드·인프라·실측)

### B-1. 손절 신뢰성 (KIS 최대 약점 보강) ★ — **코드 완료**
- ✅ **chase 로직**(R5) — `bot/kis_chase.py`(사다리·floor·취소확정 후 재발주·manual_lock).
- ✅ **파수꾼 생존성 SLA**(R4) — `bot/heartbeat.py`(30/60/120)·sentinel 배선·watchdog.
- 🧪 **생존성 훈련** — 장중 서버 재부팅·네트워크 단절 drill(서버 구축 후).

### B-2. 상시 서버 — **패키지 완료**, 실물 설치만 남음
- ✅ **서버 패키지** — `infra/server/`(sentinel.service·watchdog.service·watchdog.py·
  README 설치 절차). 👤 실물 VM 개설·설치는 사용자 작업(Oracle Always-Free 등).
- ✅ **단일 token_manager** — flock 캐시(멀티프로세스 안전) + README 운영 규칙.
- ✅ **장전 preflight 캐너리**(I5) — `scripts/kis_preflight.py`(읽기+게이트+환경 새니티).
- 🔨 **CF Worker dead-man ↔ 서버 하트비트** — 바깥 계층 연결(서버 설치 시 마무리).

### B-3. 매수 실행·사이징 — **코드 완료**
- ✅ **매수 실행기 X1** — `bot/kis_buy.py`: env→kill→boot→SLA→rollout→ownership→
  ledger→sizing→place_buy **9중 게이트 체인**(전부 fail-closed).
- ✅ **사이징 X2 + 버그 2개 수정** ★ — `bot/envelope.py`(분모 SEED·총량 게이트).
- ✅ **시드 봉투 IS3 원가축** — `bot/costbook.py`(lot cost_krw·fx고정·proceeds 환입).
- ✅ **매수여력 조회** — `kis.buying_power`(live psamount / mock 미지원=차단·명시값만).
- 🧪 **통합증거금 결정** — cash-only 실측 확인 or 사전환전 정책(기본 OFF 유지).

### B-4. 진입 게이트·안전 — **코드 완료**(종목상태 코드값만 실측 대기)
- ✅ **세션 게이트 I1** — `rollout.us_regular_open`(US 정규장만·dayMarket hard-off).
  🧪 종목 상태(상장폐지·정지) 코드값은 [대조필요] — 실측 후 게이트에 추가.
- ✅ **kill-switch L0~4**(I6) — `bot/kill.py`(latch·operator ack·allows 매핑·감사 로그).
- ✅ **환경분리 플래그**(I4) — `ALLOW_BUY`(X1)·`KIS_ORDERS_ENABLED`·`TRADE_STAGE`·
  `ALLOWED_SYMBOLS`·`BOT_SEED_KRW`. (`ALLOW_SELL`은 파수꾼 dry-run 플래그가 담당.)
- ✅ **계좌 격리 IS2/IS5** — `bot/ownership.py`(baseline denylist fail-closed·불축소·
  claim>broker 동결·sell_cap).

### B-5. 계좌·키 (사용자 작업)
- 👤 **봇 전용 실전 계좌 개설 + 전용 appkey**(IS1-A 물리격리) — 기존 자산 0 확인.
- 👤/🔨 **깃·CF에서 live appsecret 제거 확인**(I4 No-Go) — 주문 키는 상시 서버에만.

## C. Stage 2 — 실주문 (점진)
- ✅ **단계적 롤아웃 가드**(I7) — `bot/rollout.py`(Stage 1.5/2/2.5/3 프로파일 코드 강제:
  1종목·하루1건·risk 0.1%·allowlist 필수·whole-share·원장 기반 일일 카운트).
- 🧪 **실전 소액 실측 트랙** — 모의 미지원분(시장가 손절·매수여력·주간거래) 첫 검증.
- ✅ **정산 회계(최소)** — `costbook`(수수료·fx 포함 원가축). 🧪 세금·스프레드 실측치 반영.

## D. 선택/나중 (Stage 2 후반~3)
- 🔨 WS 체결통보(`H0GSCNI0/9`) — 대사 1차 채널(폴링 부하↓). source of truth 아님.
- 🔨 주간거래(dayMarket) 지원 · 기업행위 자동감지 · 조건주문(국내만).

---

## 🧪 실측·대조필요 (별도 축 — 장/키 있을 때 수확)
모의 왕복 TR/nccs/ccnl 표기 · MGCO_APTM_ODNO 에코 · ODNO없는 완전체결 ccnl 특정 ·
레이트리밋 단위(앱키/계좌) · 1키=1계좌 스코프 · 소수점 주문 가능여부 · 통합증거금 신용성격 ·
미국장 중 API 점검시간 · 모의 체결 시뮬 상세.

## 요약 — 상태 (2026-07-11 밤 갱신)
**장 없이 가능한 코드는 전부 완료**(A 코드·B-1·B-2 패키지·B-3·B-4·C 가드) —
테스트 20개 모듈 green. 남은 것은 전부 실측/실물:
1. 🧪 모의 왕복(월 22:30~) → 2. 👤 서버 VM 설치(infra/server/README) →
3. 🧪 통합증거금·종목상태 실측 → 4. 👤 봇 전용 실전 계좌+appkey → 5. Stage 2 Go/No-Go.
