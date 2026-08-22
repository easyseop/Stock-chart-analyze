# Claude 부분 재검토 요청 — F4 핫루프 분리 · SELL 전용 · 연속 갭 확인

작성: Codex, 2026-08-23

브랜치: `codex/ack-zero-fill-stale-before`

기준 HEAD: `7da43f90`

수정 커밋: `f1461e71`

선행 판정: `docs/CLAUDE_REVIEW_ACK_ZERO_FILL_VERDICT.md`의 P1 1 · P3 2

## 1. 수정 범위와 선택

G1은 지시서 권장안 **① 핫루프 밖으로 이동**을 선택했다. 데드라인 스레드는
쓰지 않았다. F4의 6회 KIS 조회가 파수꾼 heartbeat·손절 판단과 자원을 공유하지
않게 만드는 것이 결함의 직접 해소이고, 호출 하나가 최악 75초인 상태를 파수꾼
안에서 스레드로 감싸면 남은 daemon 작업과 KIS 유량 경합을 새로 만들기 때문이다.

변경 파일은 아래 다섯 개뿐이다.

- `bot/protection_observability.py`
- `bot/ops_status.py`
- `bot/kis_telegram.py`
- `tests/test_protection_observability.py`
- `tests/test_ack_unit_mismatch.py`

F1·F2·F3 판정, `scripts/kis_ack_resolve.py`, 발주 clamp, 주문·kill·동결 코드는
수정하지 않았다.

## 2. G1 — F4를 ops 주기 루프로 이동

`protection_observability.check()`는 이제 원장만 읽는
`audit_blocked_protection()`(F3)만 호출한다. `sentinel.check_once()` 배선은
그대로여서 F3은 fresh broker truth가 있는 열린 시장에서 매 사이클 수행된다.

신규 `ops_status.maybe_audit_sellable_gaps()`는 텔레그램 읽기 전용 프로세스가
매 루프 호출하고, 기존 `SELLABLE_GAP_AUDIT_S`(기본 600초)를 자체 적용한다.
열린 시장이 없으면 조회하지 않으며 실패에도 간격을 적용해 API 폭주를 막는다.
보호원장의 양수 포지션만 감사 대상으로 넘기고 통화가 KRW/USD가 아니면 시장을
추측하지 않고 판정을 보류한다.

이 프로세스가 죽으면 `telegram.service`의 `Restart=always`가 10초 뒤 재시작하고,
설치된 telegram 유닛의 inactive 상태는 `health_beacon.sh` 기본 `BEACON_UNITS`가
down으로 보고한다. F4가 오래 걸리면 텔레그램 진단 응답은 늦을 수 있지만
sentinel heartbeat·손절 판단·주문 경로는 전혀 기다리지 않는다.

실제 spy 증거:

```text
sentinel.check_once 1회
  audit_blocked_protection(F3) = 1회
  audit_sellable_gaps(F4)      = 0회
  kis.holding_quantities       = 0회(F4 경로 spy)
  kis.open_orders              = 0회(F4 경로 spy)

ops_status.maybe_audit_sellable_gaps 2회(1초 간격)
  audit_sellable_gaps(F4)      = 1회(간격 억제 확인)
```

## 3. G2 — `operator-zero-fill` SELL 전용 회귀

운영 코드는 이미 올바르므로 바꾸지 않았다. 독립 테스트
`test_operator_zero_fill_branch_is_sell_only`를 추가했다.

```text
BUY + submitted + filled=0 + age=601s + nccs 부재
+ ccnl 단일 0체결행 + fresh holding 성공
→ side=BUY, kind=hold, resolvable=false, zero_fill_proof=false
```

따라서 `side == "SELL"` 제한 제거 mutation은 이 단언에서 실패해야 한다.

## 4. G3 — 같은 갭 N회 뒤 경보

`SELLABLE_GAP_CONFIRMATIONS`(기본 2, 범위 1~10)를 추가했다. 기존 래치 파일의
`sellable_gap_counts`에 종목별 `total:sellable:open_sell` 서명과 횟수를 원자·
fsync 저장한다.

- 같은 서명이 1회 관찰되면 침묵
- 2회째 기존 P0 문구로 경보
- 갭이 사라지거나 열린 시장에서 서명이 바뀌면 0/1회로 리셋
- 닫힌 시장의 카운터는 증가·삭제하지 않음
- 전송 실패 시 알림 래치는 잠그지 않고 다음 감사에서 재시도
- 카운터 저장 실패 시 경보·회복 0, 기존 파일 byte 동일
- 이미 경보된 심볼의 갭 서명이 바뀌어도 1회차에 거짓 회복시키지 않음

기본 10분 감사 간격이므로 INGR 같은 영구 누수의 최초 P0는 이전보다 약 10분
늦어진다. 대신 1회성 장중 예약은 경보하지 않는다. F3의 30분 보호 차단 P0는
이 지연과 무관하게 매 파수꾼 사이클 계속 돈다.

## 5. 실패·불신 계약

F4의 완전 응답·부분 반환 금지·판정식은 바꾸지 않았다. 잔고/nccs 한 곳이라도
실패하면 `_collect_sellable_snapshot()`은 계속 `None`이고 카운터·알림·회복
전부 전이하지 않는다. 카운터 원자 저장 실패도 같은 판정 보류다.

## 6. 검증 증거

```text
python -m tests.test_ack_unit_mismatch                 rc=0
python -m tests.test_protection_observability          rc=0
python -m tests.test_sentinel                          rc=0
python -m tests.test_sentinel_heartbeat_progress       rc=0
python -m tests.test_ops_status                        rc=0
python -m tests.test_kis_telegram                      rc=0
python -m tests.run_all                                rc=0
ALL PASS: Python test modules 74
python -m compileall -q bot scripts tests              rc=0
/Users/seop/.nvm/versions/node/v24.15.0/bin/node \
  --test tests/site_math.test.js                       rc=0 (19/19)
/Users/seop/.nvm/versions/node/v24.15.0/bin/node \
  --check scanner/site_app/app.js                      rc=0
git diff --check                                       rc=0
```

환경 주의: 셸 기본 `/usr/local/bin/node`는 삭제된 ICU 71을 참조하는 오래된 Node
19라 코드 로드 전 `dyld` rc=134였다. 저장소 변경과 무관하며 설치된 Node 24.15로
동일 명령을 재실행해 19/19와 문법검사 rc=0을 확인했다.

## 7. 부분 재검토 요청

1. sentinel의 모든 경로에서 F4와 그 6회 KIS 호출이 정말 0인지.
2. ops 주기 함수가 열린 시장·10분 간격·통화 불신을 fail-closed로 처리하는지.
3. telegram 프로세스 사망이 systemd 재시작과 health beacon down으로 드러나는지.
4. F3이 여전히 매 파수꾼 사이클 돌고 주문 원장 외 블로킹 I/O가 없는지.
5. G3가 재시작·전송 실패·저장 실패·닫힌 시장에서 잘못 전이하지 않는지.
6. `side == "SELL"` 제거 mutation이 신규 G2 테스트에서 KILLED되는지.
7. F1/F2/F3·safe_qty·baseline·kill·동결 경로 diff가 0인지.

P0~P3로 판정하고 P0/P1이 하나라도 있으면 병합 차단을 요청한다. mutation 재주입은
검토자가 격리 worktree에서 수행한다.

## 8. 금지선

기본 브랜치 병합, Oracle 배포, 운영 `kis_ack_resolve --apply`, kill/env 변경은
수행하지 않았다.
