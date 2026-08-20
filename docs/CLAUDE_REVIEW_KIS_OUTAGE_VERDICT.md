# Claude 적대 검토 판정 — quiesce P1 재검증 + KIS 장애 오귀속 제거

검토일: 2026-08-21 · 대상: `codex/kis-outage-classification-v2` @ `a88ed7a`
(구현 `a5d2ace`·`a248d57`, base `b5b5135`) · 요청: quiesce 3문 + 반증 9문

## 판정: **P0 0 · P1 0 · P2 0 · P3 3 — 병합 가능(사용자 승인 대기)**

## quiesce P1 재검증 (3문 전부 HOLDS)

1. **다른 enablement 우회 없음** — guardian 경로는 `is-enabled == "disabled"`
   완전일치만 통과. `static`·`linked`·`indirect`·`enabled-runtime` 등 어떤
   잔여 활성화 상태도 "disabled"가 아니므로 거부(보수 방향). 코드 확인.
2. **transient 상태 오인 없음** — guardian 정확히 `inactive`만, 대상 유닛
   is-active도 `inactive` 완전일치. deactivating/failed/unknown 거부 테스트 실존.
3. **masked 경로 무퇴행** — mask 증명 분기 불변 + 회귀 테스트 유지.

## 반증 9문 — 전부 HOLDS (검토자 독립 프로브 25건 + 코드 검증)

| 문 | 판정 근거(프로브 실측) |
|---|---|
| 1 오염→오분류 | 12개 변형(손상 JSON·bool/float/음수 카운트·빈 원인·노후·미래시각·성공-최신) 전부 None 또는 outage=False. 미래시각은 False가 아니라 **None(불신)** — 요구보다 보수적 |
| 2 상향 누락/하향 | outage 분류돼도 `sla == HARD_DISABLE`에서 `raise_level(1, BALANCE)` 실행(코드) — kill 하향 경로 없음, self-heal cycle은 분류와 무관하게 매 주기 실행 |
| 3 2회째 우회 | 프로브: 2회째도 관찰 0부터 재시작·readiness 요구, 3회째 blocked·L1 유지, HEARTBEAT는 2회째부터 거부 |
| 4 예산 부활 | 레거시 `used=true` 강등 주입 → BALANCE도 소진 취급(manual_alert). 카운터 bool/float/음수/한도초과 = 손상 fail-closed(코드) |
| 5 타임아웃 오염 | `timeout=_get_timeout_s()` 사용처 `_get` 1곳뿐, `_post`류는 `_HTTP_TIMEOUT` 유지. HTTP 500 → backoff 미설정, URLError(TimeoutError)만 인정. 15→5→(성공)→15 실측 |
| 6 생존 위장 | `_beat`는 루프 내 블로킹 호출 **직전** 기록 — 스레드 0(테스트가 금지 단언 유지). 호출이 멈추면 다음 beat도 없음 |
| 7 쓰기 실패 완화 | `_write_status` 실패는 무해 삼킴 → 상태 노후 → 읽기측 노후 검사로 분류기 불가 → **기존 재시작 경로 보존**(fail-closed 방향) |
| 8 유출 | watchdog이 원인 문자열을 charset(영숫자+`_:.-`)로 재검증 — `"AAPL 74주 $65 <script>"` 주입 프로브 → 거부 실측. 심볼·금액·키 기록 없음 |
| 9 경로 오염 | diff 대상 9파일에 주문 생성·매도 판단·kill 하향 코드 없음(파일 목록·코드 확인) |

## 뮤테이션

- Codex 8종(M1~M8): 방어 코드 실존을 확인하고 증거 수용.
- **검토자 자체 4종 추가 주입**: X2(레거시 공짜 예산) KILLED ·
  **X1·X4·X5 생존** → 아래 P3.

## P3 (비차단 — 전부 "방어는 있는데 테스트가 안 지키는" 부류)

1. **X1** watchdog 원인 charset 가드 단독 제거 미검출 — 주입 방어의 회귀 테스트
   1건 권장(프로브 문자열 재사용 가능).
2. **X4** `record_failure`의 연속성 리셋(간격>300s·성공-최신 시 0으로) 제거
   미검출 — 실해악은 record_success 리셋이 사실상 겸하고 있어 미미하나,
   문서화된 계약이므로 고정 권장.
3. **X5** "outage 시 L1은 HARD(120s)에서만" 타이밍 계약 미고정 — 뮤턴트는 90s
   조기 상향(fail-closed 방향이라 위험은 아님). 타이밍 테스트 1건 권장.

셋 다 후속 처리로 충분하며 병합 차단 사유가 아니다.

## 회귀

`tests.run_all` **70모듈 ALL PASS**(검토자 독립 재실행) · 집중 9모듈 개별 PASS.

## 배포 후 운영 완료 기준(요청서 그대로)

다음 실제 KIS 버스트에서: 재시작 0 · BALANCE 사유 L1 · 30분 뒤 자동 L0 ·
복구 알림 1건. 같은 날 3번째부터는 수동(설계).
