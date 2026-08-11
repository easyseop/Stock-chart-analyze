# Claude 부분 재검토 요청 — PR #115 차단 3건 수정

대상: `codex/observability-safety` · 1차 판정 기준 `baad2c3c`

병합·Oracle 배포·kill 하향은 이 재검토와 별개이며 수행하지 않았다.

## 1. P1-1 잔고 실패 사건 단일화

- 파일: `bot/balance_health.py`, `tests/test_balance_alert_hygiene.py`
- 원인이 바뀌어도 `_incident`를 재생성하지 않는다.
- 사건 안의 `Counter`에 원인을 누적하고 모든 사건 알림에 최다 원인·횟수·원인
  가짓수를 표시한다.
- 회귀: 타임아웃/HTTP 500을 30초 간격으로 10회 교대해도 첫 경보 1건이며,
  1800초 억제창이 지난 뒤에만 두 번째 경보가 난다.

## 2. P2-1 pending 알림 귀속 확인

- 파일: `bot/kill_self_heal.py`, `tests/test_kill_self_heal.py`
- `pending_notice` 전달 직전에 현재 L0 상태의 `kill.status().who`가 정확히
  `self-heal`인지 확인한다.
- 다른 주체이면 pending을 영속적으로 폐기하고 `kill-self-heal: pending notice
  discarded` 로그를 남긴다. 알림은 전송하지 않는다.
- 회귀: pending 저장 직후 하향 전 크래시 → 운영자 수동 L0 → 자동복구 알림 0건,
  pending 빈 문자열 확인.

## 3. P2-2 완전일치 계약 고정

- 파일: `tests/test_kill_self_heal.py`
- 정확한 `watchdog` + 허용 사유 두 개만 통과한다.
- who/why 접두·부분·앞뒤 공백·대소문자 변형 9개를 모두 거부한다.

## 실행 증거

```text
balance alert hygiene H1-H3 7/7 + H4 4/4 PASS
kill-self-heal: pending notice discarded — L0 owner=operator
kill self-heal 9/9 PASS
```

- `python3 -m compileall -q bot tests` — 종료코드 0
- `git diff --check` — 종료코드 0
- 추가 핵심 회귀와 GitHub CI 결과는 같은 PR의 최신 커밋에서 확인한다.

P3 3건은 비차단 선택사항이므로 이번 부분 재검토 diff에는 포함하지 않았다.
