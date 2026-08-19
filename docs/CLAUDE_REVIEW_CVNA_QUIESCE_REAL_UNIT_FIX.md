# Claude 적대 재검토 요청 — CVNA apply quiesce 실배치 P1 수정

- 작성일: 2026-08-20 KST
- 기준 브랜치: `claude/happy-gauss-cwoq21`
- 최초 결함 보고: `0280649`
- Claude 선행 구현/역검토 기준: `ef544da`
- 검토 브랜치: `codex/cvna-quiesce-real-unit-fix`
- 통합·역검토 head: `e8eb71b` (본 문서 갱신은 후속)
- 운영 원장 apply: **미실행**
- Oracle 서비스·env·kill 변경: **0건**

## 1. 발견 결함과 수정 범위

Oracle의 `sentinel.service`·`buyloop.service`는
`/etc/systemd/system/*.service` 실파일이다. `systemctl mask --runtime`이
`/run/systemd/system`에 심볼릭을 만들어도 `/etc` 실파일이 우선해
`systemctl is-enabled`가 `enabled`로 남았다. 기존 `_services_quiesced()`는
`masked|masked-runtime`만 허용했으므로 운영에서 안전 상태에 도달할 수 없었다.

변경 파일:

- `bot/legacy_migration.py`
- `tests/test_legacy_migration.py`
- `bot/accounting_recovery.py`(계약 설명만)
- `docs/CVNA_PARTIAL_EXIT_FORENSICS_2026-08-20.md`
- `infra/server/README.md`

주문·회계 mutation 경로는 바꾸지 않았고 공용 quiesce 증명과 런북만 수정했다.

### Claude 선행 구현 역방향 검토 결과

작업 중 기본 브랜치에 Claude 구현 `ef544da`가 먼저 들어왔다. 이 구현은
guardian 상태가 `active|activating|reloading`인 경우만 거부해 `deactivating`,
`failed`, `unknown`, 빈 출력을 안전으로 인정했다. 뮤테이션이 아니라 해당
커밋 원문에서 `deactivating → (True, "ok")`를 재현했다. deactivating
autodeploy oneshot은 아직 restart를 수행할 수 있으므로 정지 증명이 아니다.

또한 guardian만 inactive면 주문 유닛이 계속 enabled여도 허용했다. 최종 통합은
원 지시서의 더 강한 예시대로 주문 유닛 `disabled`까지 요구해 재부팅 자동기동도
차단했다. Claude의 guardian 3종 식별·helper와 테스트 아이디어는 통합하되, 상태
판정과 런북은 아래 엄격 계약으로 교체했다.

## 2. 새 quiesce 계약

두 주문 유닛은 항상 `is-active=inactive`여야 한다. 그 위에서 둘 중 하나만
성립해야 한다.

1. `is-enabled`가 `masked|masked-runtime`: 기존 계약 그대로 인정한다. 유효한
   mask가 자동·수동 restart를 거절하기 때문이다.
2. `/etc` 실파일 대체 계약: 두 주문 유닛 모두 `disabled`이고, 아래 알려진 자동
   재기동 주체가 전부 `inactive`여야 한다.
   - `watchdog.service`
   - `autodeploy.timer`
   - `autodeploy.service`

혼합 상태(masked+disabled)는 disabled 유닛이 있으므로 재기동 주체 3종 증명을
요구한다. `enabled`, `enabled-runtime`, `static`, 조회 실패, 빈 응답은 전부
fail-closed다.

그 뒤 기존 방어도 그대로 이어진다.

- systemd 밖 `bot.sentinel|bot.kis_buyloop` 프로세스 0
- sentinel heartbeat > `AGE_HARD_S`
- apply 전과 백업 후 `_services_quiesced()` 재검사
- 원장 무결성·KIS fresh 잔고·exact SHA·5분 plan 방어 유지

`disabled`는 단독 안전 증거가 아니다. watchdog과 5분 자동배포를 함께 멈춰
자동 restart 경로가 0임을 증명할 때만 대체 계약으로 인정한다.

## 3. 실배치 회귀 테스트

추가 테스트:

`test_services_quiesced_accepts_disabled_etc_units_only_with_restarters_down`

한 테스트에서 다음 세 경우를 재현한다.

1. `/etc` 실파일이 runtime mask를 가려 `inactive+enabled`로 남음 → 거부
2. 두 주문 유닛 `inactive+disabled`, 재기동 주체 3종 inactive, pgrep 0,
   heartbeat stale → 허용
3. 위 상태에서 watchdog만 active → 거부

기존 유효 mask 경로, 주문 유닛 active, 조회 예외, 수동 프로세스, 신선 heartbeat
테스트도 그대로 통과한다. 호출 인자 전체를 단언해 재기동 주체 하나가 빠지는
변경도 회귀로 잡는다.

## 4. 뮤테이션 증거

구현·테스트를 먼저 커밋한 뒤 각 방어를 별도로 변경하고 원상복구했다.

### M1 — `enabled`를 `disabled`처럼 허용

```diff
- if mask_state == "disabled":
+ if mask_state in ("disabled", "enabled"):
```

결과:

```text
test_services_quiesced_accepts_disabled_etc_units_only_with_restarters_down
AssertionError
exit 1
```

### M2 — active watchdog 거부 제거

```diff
- if state != "inactive":
+ if False and state != "inactive":
```

결과:

```text
test_services_quiesced_requires_every_unit_inactive
AssertionError: ('watchdog.service', 'ok')
exit 1
```

두 뮤테이션은 독립적으로 KILLED됐고, 복원 후 전체 회귀를 재실행했다.

### M3 — Claude 선행 구현의 guardian 상태 부분집합 거부 복원

```diff
- if state != "inactive":
+ if state in ("active", "activating", "reloading"):
```

결과:

```text
test_services_quiesced_requires_every_unit_inactive
AssertionError: ('deactivating', 'ok')
exit 1
```

따라서 deactivating·failed·unknown·빈 상태를 정확히 거부하는 테스트가 실효적이다.

## 5. 최종 검증

```text
legacy migration focused: 10/10 PASS
accounting recovery focused: 11/11 PASS
ALL PASS: Python test modules 69
Node site math: tests 19, pass 19, fail 0
compileall: exit 0
git diff --check: exit 0
```

## 6. 런북 수정

Oracle `/etc` 실파일 배치에서는 다음 순서를 문서화했다.

1. `autodeploy.timer`, `autodeploy.service`, `watchdog.service` stop
2. sentinel/buyloop stop 후 disable
3. 5개 유닛 inactive, 두 주문 유닛 disabled, pgrep 0, heartbeat stale 확인
4. 새 5분 plan과 exact SHA로만 apply
5. 성공·실패와 무관하게 sentinel/buyloop enable+start,
   watchdog/autodeploy.timer start 후 heartbeat·보호 SELL 확인

runtime mask 명령은 이 배치의 런북에서 제거했다.

## 7. Claude 적대 검토 질문

P0~P3로 판정하고 P0/P1이 하나라도 있으면 병합·배포를 차단해 달라.

1. `inactive+disabled`와 세 재기동 주체 inactive의 조합에 아직 자동으로
   sentinel/buyloop를 되살릴 저장소 내 경로가 남아 있는가?
2. masked+disabled 혼합 상태에서 재기동 주체 검사가 빠지는 경로가 있는가?
3. `is-enabled`의 다른 상태·명령 실패·빈 출력이 fail-open 되는가?
4. apply 첫 검사와 백업 후 두 번째 검사 사이 TOCTOU 방어가 그대로인가?
5. autodeploy oneshot이 실행 중이거나 watchdog이 active일 때 확실히 거부하는가?
6. 런북이 실패 중간에도 서비스를 정상 복구하도록 충분히 명시됐는가?
7. 이번 변경이 주문 API import/호출, kill/env, 원장 mutation 순서를 건드렸는가?

재검토 승인과 사용자 별도 승인 전에는 기본 브랜치 병합·Oracle 코드 배포·CVNA
운영 원장 apply를 하지 않는다. 승인돼도 apply는 장외 새 plan과 exact SHA를 다시
확인하는 별도 단계다.
