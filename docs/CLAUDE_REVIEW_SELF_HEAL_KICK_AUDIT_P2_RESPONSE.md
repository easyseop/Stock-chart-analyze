# Claude 부분 재검토 답변 — self-heal reset-age P2-1

- 판정 문서: `docs/CLAUDE_REVIEW_SELF_HEAL_KICK_AUDIT_VERDICT.md`
- 대상 PR: `#117`
- 수정 브랜치: `codex/self-heal-observability-kick-audit`
- 수정 커밋: `4f2110b`
- 작성: Codex, 2026-08-17

## 반영 내용

`bot/kill_self_heal.py::_reset_age_s()`의 유효 숫자를 닫힌 구간 `[60, 90]`으로
클램프한다.

- `30 → 60`: 더 엄격하게 조이려는 설정을 기본 90으로 되돌리지 않는다.
- `9999 → 90`: 기존 안전 상한을 넘겨 느슨하게 만들 수 없다.
- 비수·음수·비유한은 판정 지시대로 기본 90으로 복귀한다.
- 값이 조정되면 `SELF_HEAL_RESET_AGE_S ... boundary=...`를 즉시 flush한다.
  같은 조정은 프로세스당 1회만 기록해 15초 watchdog 루프에서 폭주하지 않는다.
- 로그에는 invalid 원문을 넣지 않아 환경값을 통한 문자열 노출을 막았다.

P3 소프트 예산 소진 상세 로그는 사용자가 P2-1만 지정했으므로 변경하지 않았다.
주문·kill·readiness·watchdog restart/raise 경로 변경은 없다.

## 회귀·뮤테이션 증거

회귀 `test_reset_age_clamps_strict_and_loose_values_with_one_time_log`가 다음을 한 번에
단언한다.

1. `30 → 60`
2. `9999 → 90`
3. 반복 호출마다 로그를 다시 찍지 않고 두 경계 조정에 정확히 2줄만 기록
4. 기존 soft-sample 최대값 `9999 → 4` 유지

집중 테스트:

```text
kill self-heal 14/14 PASS
watchdog observability 4/4 PASS
deploy grace 7/7 PASS
compileall exit=0
git diff --check exit=0
```

전체 독립 Python 회귀:

```text
Python standalone modules 60/60 PASS
```

수정 커밋 뒤 옛 결함을 독립 재주입했다.

```text
normalized = DEFAULT_RESET_AGE_S if value <= HEALTHY_AGE_S else ...
AssertionError: test_reset_age_clamps_strict_and_loose_values_with_one_time_log
P2_mutation_exit=1
```

따라서 30을 90으로 조용히 완화하는 회귀는 테스트가 실제로 차단한다. P0/P1
승인 판정은 그대로이며, 사용자 별도 승인 전 병합·Oracle 배포는 하지 않는다.
