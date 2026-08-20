# Claude 승인 후 P3 회귀 공백 보완

작성: Codex, 2026-08-21  
브랜치: `codex/kis-outage-classification-v2`  
Claude 판정: 기본 브랜치 `dafffe2c`의
`docs/CLAUDE_REVIEW_KIS_OUTAGE_VERDICT.md`  
판정: **P0 0 · P1 0 · P2 0 · P3 3 — 병합 가능**

## 반영 내용

운영 로직은 바꾸지 않고 Claude가 확인한 세 방어를 회귀 테스트로 고정했다.

1. `test_watchdog_rejects_untrusted_outage_cause_charset`
   - `AAPL 74주 $65 <script>` 같은 원인 문자열은 outage 근거로 거부한다.
   - 안전한 `exception:TimeoutError`는 기존대로 통과한다.
2. `test_outage_failure_streak_resets_after_gap_and_success`
   - 마지막 실패에서 정확히 301초 뒤 실패는 새 연속수 1로 시작한다.
   - 성공 뒤 첫 실패도 연속수 1이며 outage가 아니다.
3. `test_outage_l1_waits_for_hard_disable_boundary`
   - heartbeat 95초/P0에서는 KIS outage여도 재시작만 생략하고 L1은 올리지 않는다.
   - 기존 hard-disable 경계인 121초에서는 정확한 BALANCE 사유로 L1을 올린다.

## 독립 mutation 재주입

테스트 커밋 `21ef7617`을 먼저 만든 뒤 Claude의 생존 변이를 각각 독립 적용·실행·
원복했다.

| 변이 | 실패 지점 | 결과 |
|---|---|---|
| X1 watchdog charset 검사 제거 | `test_watchdog_rejects_untrusted_outage_cause_charset` | KILLED, exit 1 |
| X4 5분/성공 연속성 리셋 제거 | `test_outage_failure_streak_resets_after_gap_and_success` | KILLED, exit 1 |
| X5 P0에서도 조기 L1 | `test_outage_l1_waits_for_hard_disable_boundary` | KILLED, exit 1 |

X1은 오염 원인이 `None`이어야 한다는 assertion, X4는 301초 뒤 연속수가 1이어야
한다는 assertion, X5는 95초에 `raise_level`이 호출되지 않아야 한다는 assertion에서
각각 종료코드 1을 냈다.

## 최종 회귀

- `python3 -m tests.test_kis_outage_classification`: **10/10 PASS**
- 전체 Python: **70/70 modules PASS**
- 웹 계산: **19/19 PASS**
- `node --check scanner/site_app/app.js`: PASS
- `python3 -m compileall -q bot infra tests`: PASS
- `git diff --check`: PASS

## 상태와 금지선

Claude가 보고한 P3 세 건까지 테스트 공백이 닫혔다. 구현 코드는 승인받은
`a88ed7a2`와 동일하고 테스트·문서만 추가됐다. 기본 브랜치 병합·Oracle 배포·kill
하향·env 변경은 수행하지 않았다. 사용자 병합 승인 후에만 기본 브랜치에 병합한다.
