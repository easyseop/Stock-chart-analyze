# Claude 부분 재검토 제출 — KIS 연속페이지 P1/P2 대응

대상 PR: #116 · 브랜치 `codex/balance-pagination`

기준 판정: `docs/CLAUDE_REVIEW_BALANCE_PAGINATION_VERDICT.md` (`c52998ba`)

범위는 판정에서 요청한 P1-1과 P2-1 두 건뿐이다. P3 두 건(페이지 간 중복행,
행 키 불일치)은 선택사항이므로 이번 부분 재검토 diff에는 포함하지 않았다.

## P1-1 — R0 Oracle mock 실측 완료

Oracle의 실행 중 buyloop 환경이 `KIS_ENV=mock`임을 확인한 뒤 NASD 해외잔고
endpoint만 읽기 전용으로 직접 호출했다. 주문·취소·kill·원장·서비스 상태 변경은
없었다.

| 구분 | HTTP | rt_cd | msg_cd | 행 | FK200 | NK200 | 응답 tr_cont |
|---|---:|---|---|---:|---|---|---|
| 1페이지 | 200 | 0 | 20312000 | 30 | 빈 값 | WDAY | M |
| 2페이지 (`tr_cont=N`) | 200 | 0 | 20310000 | 1 | 빈 값 | 빈 값 | D |

판정: KIS mock은 연속조회를 지원한다. 분기 A이므로 기존 페이지 루프를 mock/live
공통으로 유지한다. 실측 1페이지 한도는 30행, 당시 NASD 합계는 31종목이다.
미지원 분기 B의 80% 경보는 불필요하다. 원문 증거는
`docs/CODEX_SPEC_BALANCE_PAGINATION.md` §7에 추기했다.

## P2-1 — 외부 완전성 마커 신뢰 경계 고정

- `bot/kis.py`의 `_get`이 응답을 수신한 직후 이름이 `_pagination`으로 시작하는
  모든 키를 제거한다. 정상/HTTPError 본문 모두 분류 전에 같은 경계를 거친다.
- `_pagination_complete`와 `_pagination_pages`는 `_get_all_pages`가 모든 페이지를
  성공적으로 소진한 뒤에만 새로 생성한다.
- 신규 `test_external_response_cannot_forge_pagination_completeness`는 외부 응답에
  complete/pages/임의 vendor marker를 넣고도 `_get` 반환값에 하나도 남지 않으며,
  15행 포화 응답이 SELL 부재증명에서 계속 `None`인지 단언한다.
- 외부 마커 제거 호출을 삭제한 M5 변이는 위 테스트가 종료코드 1로 실패한다.

## 검증 증거

- `python3 -m tests.test_balance_pagination`: 9/9 PASS, 종료코드 0.
- SELL 거절 대사·KIS 어댑터·L0 readiness 집중 회귀: 모두 종료코드 0.
- Codex 번들 Python `python -m tests.run_all`: **57/57 ALL PASS**, 종료코드 0.
- `python3 -m compileall -q bot tests`, `git diff --check`: 종료코드 0.
- M5(외부 마커 제거 호출 삭제):
  `test_external_response_cannot_forge_pagination_completeness`의
  `assert not any(...startswith("_pagination"))`가 `AssertionError`, 종료코드 1.

## 재검토 요청

P1-1과 P2-1만 부분 재검토해 P0/P1/P2 폐쇄 여부를 판정해 달라. 기본 브랜치 병합과
Oracle 코드 배포는 사용자 별도 승인 전 금지이며, R0 외에는 Oracle 상태를 바꾸지
않았다.
