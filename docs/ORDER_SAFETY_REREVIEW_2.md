# 2차 최종 재검토 요청 — 취소 재시도 P0·총시드 전환 P1

마지막 갱신: 2026-07-25
기준 커밋: `106065d2`
로컬 브랜치: `codex/p0-order-protection`

## 최종 판정

2026-07-25 외부 적대적 재검토 결과 **승인**됐다.

- P0 E 취소 재시도: 확정 거부 뒤 `#N+1` 재전송, `submitted/unknown` 이중 HTTP
  차단, 취소 성공 직후 크래시 복구, 프로세스 경합 중 정확히 1건 전송 확인.
- P1 Q1 총시드 전환: terminal BUY의 미회계 예약 유지, 최종 flock 안 durable
  costbook 재조회, 손상 fail-closed, 회계 직전 크래시 재시도 멱등 확인.
- 교착·기존 7개 주문 불변식 회귀 없음.
- 병합 및 Oracle 모의계좌·L1 유지 단계배포 승인.

비차단 P2로 “브로커가 체결가를 계속 주지 않으면 `filled > accounted` 예약이 오래
남을 수 있음”이 제시됐다. 코드의 fail-closed 방향은 유지하고 다음 운영 감시를
추가했다.

- 매수 루프가 미회계 BUY를 매 사이클 확인한다.
- 같은 `filled/accounted` 차이가 기본 3회 연속이면 치명 운영 알림을 1회 보낸다.
- 예약은 계속 유지하며 주문·취소·게이트는 바꾸지 않는다.
- 회계가 끝나면 감시 상태를 자동 제거한다.
- `KIS_ACCOUNTING_ALERT_CYCLES`로 2~60회 범위에서 임계값을 조정할 수 있다.

## 검토 당시 상태

이 변경은 아직 커밋·push·Oracle 배포하지 않았다. Oracle은 기준 커밋에서
kill-switch L1(`buy_new=False`, `protect_sell=True`)을 유지한다. 아래 두 차단 결함의
재검토 승인 전에는 병합·배포·L1 해제를 하지 않는다.

직전 외부 재검토 판정은 다음과 같았다.

- 기존 18건 중 16건 `HOLDS`
- P0 E: 취소키 고정으로 1회 확정 실패 뒤 보호 손절 영구 정지
- P1 Q1: BUY `filled` 종료와 costbook 회계 사이 총시드 과소계상 창
- 비차단: 미체결 B 계획의 A 보유 재태깅, 첫 틱 `opened==""` 수동매수 누출

## P0 E 수정 — 확정 실패만 새 취소 시도 허용

관련 파일:

- `bot/kis_pending.py`
- `bot/kis_orders.py`
- `bot/ledger.py`
- `tests/test_kis_pending.py`

수정 내용:

1. 보호 취소키를 `원주문:protect-cxl#N`, 일반 대기취소를 `원주문:cxl#N`으로
   시도별 분리했다.
2. 같은 취소 묶음에서는 앞 시도가 `rejected`로 확정된 경우에만 다음 `#N+1`
   취소를 허용한다.
3. 앞 시도가 `submitted`·`unknown`이면 새 취소 HTTP를 차단한다. 응답유실을
   확정실패로 오인해 이중 취소하지 않는다.
4. 앞 시도가 `filled`(취소 접수 성공)이면 새 HTTP를 막는다. 성공 직후 프로세스가
   죽어 원주문을 `cancel_pending`으로 바꾸지 못한 경우, 다음 사이클이 성공 시도를
   찾아 원주문 상태만 복구한다.
5. 레이트리밋·브로커 확정 거부는 취소 시도를 `rejected`로 닫으므로 다음 사이클에
   새 키로 재시도된다.
6. 취소 확인 뒤 최신 KIS 실보유·매도가능수량만 보호 SELL로 넘기는 기존 불변식은
   그대로 유지한다.

자동 장애 재현:

- BUY 8주 중 3주 체결·5주 미체결
- 첫 취소 `#1`이 `rate_limited/rejected`
- 다음 사이클 `#2`가 취소 접수
- 브로커 미체결 소멸 확인
- 체결된 3주만 보호 SELL 단계로 인계
- 별도 `unknown` 취소에서는 다음 사이클 취소 HTTP 0건

## P1 Q1 수정 — filled 예약을 durable 회계까지 연속 유지

관련 파일:

- `bot/ledger.py`
- `bot/kis_accounting.py`
- `bot/costbook.py`
- `bot/kis_positions.py`
- `tests/test_ledger.py`
- `tests/test_kis_accounting.py`
- `tests/test_kis_buy_gates.py`

수정 내용:

1. BUY가 `filled/cancelled` 종료 상태여도 `filled > accounted`인 수량은 총시드
   예약에서 제거하지 않는다. 부분체결 종료는 미회계 수량 비율만 유지한다.
2. 최종 `try_record_submit` flock 안에서 durable costbook을 다시 읽는다. 호출부가
   flock 밖에서 만든 오래된 브로커 원가를 넘겨도 둘 중 큰 값을 held 바닥으로 쓴다.
3. costbook은 프로세스 공용 flock, `O_APPEND`, 파일 `fsync`, 최초 생성 시 디렉터리
   `fsync`를 사용한다. JSON 손상·읽기 오류면 예산 스냅샷을 `None`으로 반환해
   신규매수를 차단한다.
4. `sync_fill` 전체를 주문원장 flock으로 직렬화한다. costbook과 보호 포지션 기록이
   끝난 뒤에만 같은 임계구역에서 `accounted`를 기록한다.
5. costbook·KIS 보호 포지션에 체결 누적수량별 `event_id`를 넣었다. durable 기록 뒤
   `accounted` 직전 프로세스가 죽어도 재시도가 같은 lot/수량을 중복 반영하지 않는다.
6. KIS 보호 포지션 파일도 프로세스 잠금과 파일·디렉터리 fsync를 사용한다.

자동 장애 재현:

- 원장 BUY 10주가 `filled`, costbook/accounted는 아직 0
- 다른 프로세스가 새 BUY를 요청하면 기존 10주 예약으로 차단
- costbook 기록 후 호출부가 여전히 held=0인 오래된 스냅샷을 넘겨도 flock 안에서
  costbook 원가를 다시 읽어 초과 주문 차단
- costbook JSON 손상 시 신규매수 차단
- costbook·보호 포지션 기록 뒤 `accounted` append에 강제 `OSError`
- 재시작 재회계 후 costbook 수량 3주, 보호 포지션 3주로 유지(중복 0)

## 비차단 2건도 수정

1. 미체결 `planned` B 주문은 기존 A 보유를 B로 재태깅하지 않는다. 실제 `filled`
   수량 또는 `hldg_before` 대비 브로커 잔고 증가가 있을 때만 임시 슬리브 귀속을
   바꾼다. B 계획 예약 자체는 B 예산에 계속 포함한다.
2. 장 시작 동일가중 종목은 `opened`가 존재하고 `opened < session_day`인 추적
   포지션만 포함한다. 첫 틱 전에 들어온 출처 불명 수동매수(`opened==""`)도 제외한다.

추가로 pandas 신·구 버전의 월말 별칭 `ME/M`을 모두 지원하도록 테스트 픽스처를
호환 처리했다. 운영 계산은 기존 호환 분기를 유지한다.

## 재검토 반증 질문

1. 취소 `#1`이 확정 거부된 뒤 `#2`가 실제 전송 가능한가?
2. 취소 `#1`이 `unknown/submitted`인데 다른 프로세스가 `#2` HTTP를 보낼 수 있는가?
3. 취소 접수 성공 직후 크래시하면 다음 사이클이 새 취소 없이 원주문을
   `cancel_pending`으로 복구하는가?
4. 두 프로세스가 같은 취소 묶음의 같은 다음 번호를 계산해도 flock에서 HTTP가
   정확히 1건만 열리는가?
5. BUY가 원장에서는 terminal이지만 `accounted < filled`일 때 예약이 남는가?
6. `accounted == filled` 뒤에도 호출부 held 스냅샷이 오래됐으면 flock 안의 durable
   costbook이 총시드 과소계상을 막는가?
7. costbook 손상·읽기 실패가 부분 원가로 계속되는 fail-open 경로가 있는가?
8. costbook/보호 포지션 기록 뒤 `accounted` 전 크래시를 반복해도 lot·보호수량이
   중복되지 않는가?
9. 새 ledger→costbook 잠금 순서와 다른 호출 경로 사이 교착 가능성이 있는가?
10. 위 수정이 기존 UNKNOWN SELL 잠금, 잔여만 재주문, 취소 확인 전 SELL 금지,
    `SENTINEL_LIVE` 게이트를 약화했는가?

## 검증 결과

```bash
.venv/bin/python -m tests.run_all
.venv/bin/python -m compileall -q bot scanner tests
/Users/seop/.nvm/versions/node/v24.15.0/bin/node --check scanner/site_app/app.js
/Users/seop/.nvm/versions/node/v24.15.0/bin/node tests/site_math.test.js
git diff --check
```

- Python 독립 테스트 모듈: `41/41`
- 사이트 계산 Node 테스트: `5/5`
- Python compileall: 통과
- JavaScript 문법: 통과
- `git diff --check`: 통과

최종 요청: P0 E와 P1 Q1이 반례까지 닫혔는지 확인하고, 병합·Oracle 단계배포 가능
여부를 `승인` 또는 남은 차단 항목으로 판정해 달라.
