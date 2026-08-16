# Claude 적대 검토 판정 — 자가복구 완화·관측성·외부 킥 감사 (T1~T3)

검토일: 2026-08-17 · 대상: `codex/self-heal-observability-kick-audit` @ `4ad7409e`
(구현 `069a5a2`, 권한 보완 `4fbc7d6`, base `670dcce`)
지시서: `docs/CODEX_SPEC_SELF_HEAL_TUNING.md` T1~T3

## 판정: **승인 — P0 0 · P1 0 · P2 1 · P3 1**

P2 1건은 설정 처리 방향 결함(운영 기본값에는 영향 없음)이라 병합 차단 사유가
아니다. 병합 전 고쳐도 좋고, 후속으로 처리해도 무방하다.

## 핵심 검증 — 원래 문제가 실제로 해결되는가

검토자가 `cycle()`을 15초 간격으로 직접 구동해 5개 패턴을 시뮬레이션했다
(kill·readiness·notify는 결정론적 모의):

| 패턴 | 결과 |
|---|---|
| **① 8/14 실측형**(정상 다수 + 62~71초 단발 3회) | **30분에 복구 ✅** · reset 0회 |
| ② 만성 저하(4샘플마다 70초 반복, 100분+) | **복구 없음 ✅** · reset 20회 |
| ③ 95초 하드 저하 1회 후 정상화 | 리셋 1회 후 **33분에 복구** |
| ④ 60초 초과 연속 2회 후 정상화 | 리셋 1회 후 **33분에 복구** |
| ⑤ 소프트 3연속 후 정상화 | 리셋 3회 후 **31분에 복구** |

①이 이 작업의 목적이었고(8/14 12시간 미복구 재현 패턴), ②가 완화의 대가로
생길 수 있었던 위험(만성 저하 서버를 복구시켜 버리는 것)이다. **목적은 달성,
위험은 차단**됨을 독립 시뮬레이션으로 확인했다.

## 그 밖의 확인

- **안전선 상한 고정**: `SELF_HEAL_RESET_AGE_S=9999`·`MAX_SOFT_SAMPLES=9999`
  주입 → 각각 90.0·4로 클램프(프로브 실측). 환경변수로 느슨하게 만들 수 없다.
- **공개 판정 파일 신뢰 경계**: `cycle()`은 0644 공개 스냅샷을 **읽지 않는다**
  (코드 검증). 위조·손상은 표시만 흐릴 뿐 상태기계를 진행시키지 못한다.
  핵심 상태는 0600 유지.
- **상향·재시작은 히스테리시스 밖**: `raise_level(1, …)`·`_restart_sentinel()`은
  알림 래치와 무관한 분기에서 그대로 실행된다(코드 검증). 알림만 늦춰진다.
- **self-heal 예외 격리**: `cycle()` 예외가 watchdog 본체 루프를 깨지 않음.
- 신규·회귀 7모듈 전부 PASS(test_kill_self_heal·test_watchdog_observability·
  test_kick_audit·test_deploy_grace·test_ops_status·test_kis_telegram·
  test_killswitch).

## P2-1. 설정 클램프가 "더 엄격한 값"을 조용히 되돌린다

`bot/kill_self_heal.py:41-45` `_reset_age_s()`:

```python
return min(value, DEFAULT_RESET_AGE_S) \
    if math.isfinite(value) and value > HEALTHY_AGE_S else DEFAULT_RESET_AGE_S
```

`HEALTHY_AGE_S = 60`이므로 **60 이하 값은 전부 기본값 90(가장 느슨한 값)으로
되돌아간다.** 검토자 프로브 실측:

```
SELF_HEAL_RESET_AGE_S=30  →  _reset_age_s() = 90.0   ← 더 엄격하게 조이려 했는데 완화됨
SELF_HEAL_MAX_SOFT_SAMPLES=1 → 1                      ← 이쪽은 정상(엄격 허용)
```

Codex 요청서의 "더 엄격한 값만 허용" 서술과 실제 동작이 어긋난다. 운영자가
사고 대응 중 45초로 조이려 하면 **조용히 90초가 된다** — 잘못된 방향의 fail.
현재 운영은 기본값을 쓰므로 활성 결함은 아니다(그래서 P1 아님).

**최소 수정**: 범위 밖 입력을 기본값으로 되돌리지 말고 경계로 클램프하고
로그를 남긴다 — 예: `max(HEALTHY_AGE_S, min(value, DEFAULT_RESET_AGE_S))`,
비수·음수·비유한만 기본값. 회귀 테스트 1건(30 → 60, 9999 → 90).

## P3 (비차단)

1. `soft_over_total`은 관찰창 단위로만 리셋된다. 창이 길게 유지되는 경우
   (예: 29분 시점에 소프트 4회 소진) 이후 정상 표본만 이어져도 5번째 소프트
   1회에 전체가 리셋된다 — 의도된 보수성이지만, 로그에 `soft 4/4 소진`처럼
   남겨 두면 운영자가 "왜 또 리셋됐나"를 즉시 알 수 있다.

## 반증 질문 10개 요지

1·2 ✅(시뮬레이션 ①③④⑤), 3 ⚠️(상한은 고정되나 하한 방향이 P2-1),
4 ✅(기존 회귀 유지), 5 ✅(코드 검증 — 상향/재시작 분기 불변),
6 ✅(cycle이 공개파일 미참조·0600 유지), 7 ✅(ops·/진단은 공개 스냅샷만 읽음),
8·9 ✅(test_kick_audit 8/8 + M7·M8·M9 KILLED),
10 ✅(dispatch 경로 없음·`continue-on-error`로 기존 자가치유 독립).

---

## 부분 재검토 — P2-1 수정 (2026-08-17, `4f2110b` / 문서 `f5043669`)

### 판정: **P2-1 해소 — 신규 P0·P1·P2 0건**

변경 범위는 `bot/kill_self_heal.py`(`_reset_age_s` + `_log_config_clamp`),
`tests/test_kill_self_heal.py`, 문서 2건뿐이다(`git diff --stat` 실측).
주문·kill·readiness·상향·재시작 경로 diff **0**.

### 검토자 독립 프로브(16개 입력 실측)

| 입력 | 반환 | 로그 |
|---|---|---|
| 미설정 | 90.0 | 0줄 |
| `30` / `45` / `59.9` | **60.0** | 1줄 (`requested=… — boundary=60s`) |
| `60` / `61` / `89` / `90` | 그대로 | 0줄(조정 없음 → 침묵) |
| `90.1` / `9999` | **90.0** | 1줄 |
| `0` | 60.0 | 1줄 |
| `-5` / `nan` / `inf` | 90.0 | 1줄(`nonfinite_or_negative`) |
| `abc` / 빈 문자열 | 90.0 | 1줄(`invalid` — 원문 미노출) |

세 번 연속 호출해도 로그는 **1줄**(프로세스당 1회, `flush=True`). watchdog이
15초마다 도는데도 로그 폭주가 없음을 실측했다. 지시서의 "더 엄격한 값만
허용"이 이제 실제 동작과 일치한다.

### 새로 생긴 위험은 없는가 — 경계 소멸 확인

`SELF_HEAL_RESET_AGE_S=30`일 때 유효값이 60이 되어 소프트 구간
(`60 < age ≤ reset_age`)이 **비어 버린다**. 이것이 완화 방향의 사고를 만들지
확인했다:

```
age=59    healthy=True  soft=False  hard_reset=False
age=60    healthy=True  soft=False  hard_reset=False
age=60.5  healthy=False soft=False  hard_reset=True   ← 60 초과 즉시 리셋
age=95    healthy=False soft=False  hard_reset=True
```

소프트 관용이 사라지고 **60초 초과 단발에도 즉시 리셋**된다 = 요청한 대로
더 엄격해진다. 완화 방향 사고 없음. `MAX_SOFT_SAMPLES=9999 → 4` 상한도 유지.

### 뮤테이션 검증 (커밋 후 주입, 3/3 KILLED)

| 뮤턴트 | 결과 |
|---|---|
| M1 옛 동작 복원(범위 밖 → 기본 90) | **KILLED** exit=1 · AssertionError |
| M2 1회성 로그 억제 제거 | **KILLED** exit=1 |
| M3 상한 클램프 제거(`max(60, value)`만) | **KILLED** exit=1 |

M3까지 죽는다는 것은 새 테스트가 "엄격 방향 허용"만이 아니라 **느슨화 차단도
같이 지키고 있다**는 뜻이다. 검증 후 작업본 복구 확인(`git status` 청결).

### 회귀 재실행 (7모듈 전부 PASS)

```
test_kill_self_heal              rc=0 | kill self-heal 14/14 PASS
test_watchdog_observability      rc=0 | watchdog observability 4/4 PASS
test_kick_audit                  rc=0 | kick audit 8/8 PASS
test_deploy_grace                rc=0 | deploy grace 7/7 PASS
test_ops_status                  rc=0 | 서버 자가진단 발행 검증 통과
test_kis_telegram                rc=0 | 텔레그램 조회 봇 검증 통과
test_killswitch                  rc=0 | ✅ 긴급 정지 전부 통과
compileall(bot·infra·scripts·tests) OK
```

### 남은 P3 (비차단, 병합과 무관)

1. 기존 P3(소프트 예산 소진 로그) — 사용자가 P2-1만 지정해 미반영. 유지.
2. `-5 → 90`(기본값) vs `0 → 60`(엄격 클램프)로 처리 방향이 갈린다. 둘 다
   안전한 값이라 결함은 아니지만, 음수도 `invalid`와 같은 취급이라는 점만
   알고 있으면 된다.

**최종: PR #117 = P0 0 · P1 0 · P2 0 · P3 2 — 병합 가능(사용자 승인 필요).**

---

## 배포 주의(그대로 유효)

autodeploy 재시작 목록에 watchdog이 없으므로 **장외에 `sudo systemctl restart
watchdog` 1회**, sentinel·buyloop·telegram도 새 `PYTHONUNBUFFERED=1`을 읽도록
한 번 재시작. 이후 `journalctl -u watchdog -f -o short-iso`로 로그가 즉시
기록되는지(같은 초로 뭉치지 않는지) 확인하면 T2 실측이 끝난다.
