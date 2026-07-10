# KIS 전환 — Codex 리뷰 반영 (확정 오버라이드)

> 2026-07-10. `KIS_MIGRATION_REVIEW_SECTION_BY_SECTION.md`(Codex 섹션별 검토)를 반영한
> **확정 오버라이드**. 충돌 시 **이 문서가 `KIS_API_READINESS.md`·`KIS_MIGRATION_FROM_TOSS.md`를
> 우선**한다(토스에서 07이 01/02를 오버라이드한 것과 동일 구조).
> Codex 판정: 전 섹션 **채택/수정채택**(기각 0). 아래는 그 수정사항을 코드가 강제할 규칙으로 못박음.

---

## 1. 확신 표기 하향 (Codex Q1) — [확정]→[대조필요]로 내린다

READINESS의 다음 [확정] 표기는 **실측 전까지 [대조필요]**로 간주한다:
1. **레이트리밋 단위** — 20/s·2/s 수치는 신뢰도 높으나 **단위(앱키/계좌/상품/TR그룹/환경별)는
   Stage 1 실측**. (헤딩은 이미 "[확정 수치 / 단위 대조필요]".)
2. **1 appkey = 1 계좌** — 방향은 맞으나 **앱키가 실제로 그 1계좌만 접근하는지**(같은 로그인의
   연금/ISA/타 상품 간접접근 없는지) live `accounts/balance` 실측.
3. **개인 IP allowlist 불필요** — **영구 불변식으로 박지 말 것.** 장전 preflight 항목으로:
   `live 서버 IP에서 tokenP·잔고조회·주문 preflight 성공`을 Stage 2 Go 조건에 포함.
4. **미국주 서버측 STOP 없음** — 현재 근거상 맞으나 개발자센터 원문/실주문 가능 타입에서 최종 재확인.

**확정으로 유지 OK**(Codex 인정): tokenP JSON·24h/6h 토큰·발급 1분1회·실전/모의 키 분리·모의 환경
존재·EGW00201 존재·EGW00201=HTTP500(우리 실측).

## 2. 톤 교정 — "이미 흡수" 삭제, "조건부 순득"

"우리 아키텍처가 KIS 약점을 설계상 이미 흡수" → **과한 표현**. 정정: **KIS 미국주는 손절 신뢰성
등급이 실제로 하락**(보조 방어마저 없음). **"KIS가 더 안전"이 아니라 "계좌격리로 운영 blast
radius가 작아진다"가 정확한 전환 이유.** → READINESS §14 반영 완료.

## 3. 신설 하드룰 (코드가 강제) — Stage 2 전 구현·검증

### R1 — token_manager는 "호스트 단일" (프로세스 단일 아님) [Codex A1]
루프A/B/C·sentinel이 별도 systemd 프로세스면 메모리 락 부족 → **파일 락(flock) 기반 원자 토큰
캐시**. `token.json{access_token, expires_at, issued_at, env, appkey_hash}` + refresh 락. 락 획득
실패 시 타 프로세스 갱신 완료까지 대기. **refresh 실패는 우선 retry, 손절 경로에서 실패해야 P0**.
- 깨짐: 장 시작 시 3프로세스 동시 cache-miss→동시 tokenP→1분1회 제한→일부 실패→손절 시 토큰 없음.

### R2 — 레이트리밋 재시도를 plane별 분리 [Codex A2]
공식 백테스터의 **61초 백오프는 data-plane(조회·배치)만.** **order-plane(손절 주문·손절 상태조회)
에서 61초 대기 금지** — EGW00201 시 짧은 백오프 + 즉시 P1/P0 + 신규진입 중지, "재시도 대기"가
아니라 **"손절 집행 불능 경보"**. 모의는 sentinel 폴링을 낮추고, 실전 손절 성능은 소액 트랙 검증.

### R3 — UNKNOWN 대사 confidence + 동일종목 규칙 [Codex A6·B5·Q6-1] ★위험도 1위
합성 대사키는 정상 단일주문에만 안전, **동일종목·같은 초·같은 수량·chase 재주문에서 오매칭**.
Stage 2 강제:
- **UNKNOWN 발생 symbol은 chase 금지 + 신규/추가/정정 전부 금지**(해소 전).
- **같은 symbol/side/order_action은 UNKNOWN 해소 전 추가주문 금지**, Stage 2는 **동일 symbol 신규
  주문 간 최소 60초**.
- **symbol당 동시 open order 1개만.**
- **confidence score**: `HIGH`=단일 후보 + 수량/시간/side 일치 → 자동 해소. `LOW`=후보≥2 또는
  fills delta 불일치 → **MANUAL_REVIEW_LOCK(자동 해제 절대 금지)**.
- 대사는 **nccs + ccnl + holdings delta + ord_psbl_qty delta 모두** 대조(완전체결이 nccs에 안 뜨는 케이스).

### R4 — 파수꾼 생존성 SLA (수치 명시) [Codex A7·B4] ★위험도 2위
서버측 스톱이 없어 **파수꾼 다운 = 무방비**. SLA:
- heartbeat age **≤30초 정상 / >60초 P0 / >120초 + 보유 포지션 → 신규진입 hard-disabled**.
- **boot reconcile(O4) 완료 전 신규진입 금지.** 재시작 후 position coverage 100% 확인.
- systemd auto-restart + 동일 서버 별도 watchdog process + CF dead-man ≤60초.
- **장중 서버 재부팅 훈련·네트워크 단절 훈련**을 Stage 2 전 필수. sentinel 2분+ 다운·보유중 → 수동 런북.

### R5 — 마켓터블 지정가 chase 파라미터 [Codex B4] ★위험도 2위
연속장 시장가 부재 → 마켓터블 지정가 손절. 무한 chase·이중매도 방지:
- `max_chase_count` · `max_slippage_bps` · `max_time_to_exit` 설정.
- chase는 **가능하면 cancel/replace 대신 "미체결 잔량 확인 후 정정" 우선.**
- **정정/취소 UNKNOWN이면 그 symbol MANUAL_REVIEW_LOCK.**
- `max_time_to_exit` 초과 시 P0 + 수동 런북(자동 이중주문 금지).

### R6 — 통합증거금 Stage 2 기본 OFF [Codex A9·B3]
cash-only 원칙에 가장 민감. **Stage 2 기본값 = 통합증거금 미사용, USD 사전환전 후 외화예수금
범위 내 매수**(또는 아주 작은 KR-only). 통합증거금은 **약관/실측으로 cash-only(미수/신용 아님)
확인 후 별도 기능 플래그로만** 켠다. 켤 땐 `echm_af_ord_psbl_amt`·`itgr_ord_psbl_amt`·예상
자동환전액·환율을 원장에 기록. → MIGRATION §B3 "X3 사실상 제거"는 **보류**로 정정.

### R7 — WebSocket은 source of truth 아님 [Codex A8]
WS 체결통보는 **빠른 힌트**. 확정 대사는 항상 REST nccs/ccnl/holdings. WS 수신 후에도 O3/O4가
최종 확인. WS sequence gap·재연결 시 전체 bootstrap 대사. → **Stage 2 필수 아님**(Q4): 초기는
REST 폴링+대사로 충분, WS는 Stage 2 후반/Stage 3.

### R8 — dayMarket/pre/after Stage 2 hard-off [Codex B7]
Stage 2는 **US 정규장만.** 주간거래(모의 미지원·지정가만·엔드포인트 분리)는 Stage 3 실측 항목.

### R9 — SEED 봉투 외부교란 방지 [Codex B6]
봇 전용 계좌라도: **앱 수동주문 금지**, 매일 장전 **broker holdings ≠ 원장이면 신규진입 금지**,
**외부 입출금 감지 시 수동 승인 전까지 sizing 중지**(SEED 봉투 왜곡 차단). SEED 기준 equity는
계좌 전체가 아니라 봇 seed cap(07 IS4 그대로).

### R10 — 환경 혼동·config 스왑 사고 방지 [Codex B9·Q2]
`KIS_MOCK_*`와 `KIS_LIVE_*`가 같은 config면 **live를 기본값으로 두지 않는다.** 로그에 `appkey_hash`
+ `env` 출력(시크릿 절대 금지). 봇 전용 실전계좌는 **기존 자산 0 확인** 후 시드만 입금.

## 4. "그대로 둔다"→"확장" 정정 (Codex Q3·B1)

MIGRATION §1의 "한 줄도 다시 안 짠다"는 과함. **그대로**는 전략게이트·R배수 exit·시드봉투 개념·
ntfy P0·CF dead-man까지. **아래는 확장이 필요**하다:
- **`ledger.py`**: KIS `ODNO`·합성 대사키·nccs/ccnl reconcile **confidence**·`rt_cd/msg_cd` 상태.
- **`sentinel.py`**: 시장가 매도 가정 제거 → 마켓터블 지정가 + chase + partial residual +
  cancel/replace UNKNOWN 처리 + KIS rate limiter.
- **sizing**: 계좌 equity 아님 → **SEED 봉투**. buying-power는 **하향 clamp만**(분모 금지).
- **kill-switch**: 파수꾼이 최후 방어선이므로 **Level 3/4에서 뭘 살리고 죽일지 재정의**(보호 매도
  경로는 최후까지 유지).
- **bootstrap reconcile(O4)**: open orders만이 아니라 **nccs/ccnl/holdings 전체** 확인.

## 5. 추가 실측 목록 (Codex A12) — READINESS §12에 합산

동일종목 같은 초 2주문 대사 가능성 · `MGCO_APTM_ODNO` 조회 에코 여부 · ODNO 없는 완전체결을
ccnl로 특정 가능한지 · ccnl 조회 지연시간 · 마켓터블 지정가 chase 부분체결 잔여 처리 ·
sentinel down drill · 미국장 중 API 점검 가능성 · 외화부족/통합증거금 미신청 시 에러코드 ·
appkey가 정말 그 1계좌만 접근하는지 · 모의↔실전 TR_ID 차이 자동 테스트.

## 6. Stage 2 No-Go 추가 (Codex A13·Q5) — READINESS §13.2에 합산

- ODNO 없는 UNKNOWN을 ccnl/nccs로 특정 실측 실패
- 동일 symbol/side 다중주문 오매칭 방지 정책 없음
- 마켓터블 지정가 chase 미검증 · cancel/replace UNKNOWN 처리 미구현
- 파수꾼 heartbeat/deadman 미구현 또는 60초+ 공백 대응 없음
- 통합증거금 cash-only 미확인 · 실전 매수여력조회 미검증
- 모의 매도 TR_ID 비대칭 실측 안 됨
- **GitHub/CF에 live appsecret 존재**
- 봇 전용 계좌가 비어있지 않거나 앱 수동주문 가능성 남음

## 7. 실행 우선순위 (Codex 실행 우선순위 채택)

**지금 바로**: ① token_manager + flock ② TR_ID 명시 테이블 + 테스트 ③ 모의 프로브(token/balance/
nccs/ccnl) ④ ledger 확장(ODNO·synthetic_key·confidence·rt_cd/msg_cd) ⑤ rate limiter(20/s·2/s).
✅ ③ `scripts/kis_probe.py`로 **token/balance/nccs/ccnl 전부 모의 실측 green(2026-07-10)** —
대사 채널 A(nccs)·B(ccnl) 작동 확인. ✅ ①②④(일부) `bot/kis.py`(token flock·TR테이블·
classify_error) + `tests/test_kis.py`. 남은 ④ = ledger의 ODNO·합성 대사키·confidence 확장.

**모의 Stage 1.5 전**: order primitive(모의) · nccs+ccnl reconcile · UNKNOWN manual lock ·
same-symbol single-open-order · 모의 EGW00201/timeout/부분체결 **장애주입**.

**실전 Stage 2 전**: 봇 전용 실계좌+별도 appkey · GitHub/CF live appsecret 제거 · 통합증거금
cash-only 확인 또는 사전환전 정책 확정 · marketable limit 손절 실측 · sentinel heartbeat SLA·
재시작 훈련 · ODNO 없는 timeout 대사 실측 · 모의 매도 TR 비대칭 실측.

**Stage 2 첫 주**: US 정규장만 · whole-share만 · 동시 1종목 · 하루 1건 · risk ≤0.1% · 동일 symbol
동시주문 금지 · UNKNOWN 시 symbol manual lock · 파수꾼 heartbeat green일 때만 진입.

---

## 최종 한 문장 (Codex, 채택)
> **KIS 전환은 계좌 격리·모의 검증 때문에 맞는 방향이다. 그러나 KIS 미국주는 서버측 STOP도,
> 연속장 시장가도, 클라이언트 멱등키도 없으므로, 실전 안전성의 핵심은 KIS 어댑터가 아니라
> `UNKNOWN 대사 + 파수꾼 생존성 + 마켓터블 지정가 chase`다. 이 셋이 실측으로 green 되기 전엔
> Stage 2 실매수 자동화를 켜지 않는다.**
</content>
