# Claude 재검토 요청 — 전략 A 정체청산·성과/지수 동시 리베이스

## 요청 판정

아래 변경 전체를 적대적으로 검토해 `P0/P1/P2/P3`, 반례, 승인/차단을
판정해 주세요. `P0/P1`이 하나라도 있으면 병합하지 않습니다. 승인되더라도
Oracle 장부 apply, L1 해제, `STALL_EXIT_MODE=live`는 각각 별도 게이트입니다.

기준은 최신 기본 브랜치의 PR #92 병합 커밋 `ba30f9c7`이며, 검토 브랜치는
`codex/stall-exit-performance-rebase`입니다.

## 변경 1 — 전략 A 절반익절 뒤 정체청산

의도:

- `+1R 절반익절`이 **실제 체결 확정**된 전략 A 잔량만 대상으로 한다.
- 유효 시세를 받은 열린 시장의 고유 거래일만 하루로 센다.
- 15거래일 동안 의미 있는 신고가가 없으면 추적폭을 `1.5R → 1.0R`로 좁힌다.
- 30거래일이면 남은 수량을 기존 KIS 매도·원장·대사 경로로 청산한다.
- 직전 정체 기준보다 `+0.25R` 이상 신고가면 정체일을 0으로 되돌리고
  1.5R 폭을 복원한다. 손절선은 절대 낮추지 않는다.
- 전략 B에는 적용하지 않는다.
- KIS와 autopaper가 `bot/stall_exit.py`의 동일한 순수 상태 전이를 사용한다.
- 상태 손상은 즉시 청산하지 않고 기존 1.5R 보호를 유지하며 0일부터 다시 센다.
  같은 손상 바이트의 치명 경보는 1회이고 forensic 사본을 남긴다.
- `STALL_EXIT_MODE=off|shadow|live`; 알 수 없는 값은 `off`.
- 기본값은 `off`. `shadow`는 제안만 기록하고 추가 래칫·청산 주문은 0건이다.

특히 확인할 반례:

1. 절반익절 ACK·부분체결만으로 정체 카운트가 시작되거나 본전 래칫이 되는가.
2. 같은 KST 날짜를 여러 번 실행하면 정체일이 중복 증가하는가.
3. 휴장·장마감·시세 0/NaN이 최고가나 정체 기준을 바꾸는가.
4. 정확히 `+0.25R`, 15일, 30일 경계가 한 번만 발동하는가.
5. 신고가 리셋 때 이미 올라간 손절선이 내려가는가.
6. `shadow/off`가 신규 매도 또는 1.0R 추가 래칫을 실제로 수행하는가.
7. 30일 매도 거절·UNKNOWN·부분체결 뒤 동일/초과 매도가 가능한가.
8. 기존 열린 BUY가 있을 때 잔량 매도가 취소확인 없이 나가는가.
9. 상태 JSON 손상만으로 즉시 전량매도하거나 경보가 폭주하는가.
10. 전략 B의 VAH 목표·기존 타임스탑이 달라졌는가.
11. autopaper와 KIS의 날짜·신고가·15/30 전이가 갈라지는가.

## 변경 2 — 계좌·지수 장기 비교 기준 교정

원인:

- 기존 화면의 약 `-17%`는 실제 보유종목의 하루 폭락을 뜻하지 않았다.
- 16개 legacy BUY가 회계되지 않은 상태에서 보유 NAV와 현금흐름이 불완전했고,
  일부 종목만 수집된 동일가중 보유수익률도 전체 포트폴리오처럼 표시됐다.
- 그러므로 오염된 계좌 누적률과 정상 지수를 이어 붙이면 장기 비교 자체가
  거짓이 된다.

구현:

- 승인된 legacy migration apply가 16건을 모두 검증·accounted 처리한 **뒤에만**
  `alpha.rebase_after_accounting_migration(plan_sha)`를 호출한다.
- apply 전 원본 `alpha_state.json`을 다른 세 원장과 함께 byte-for-byte 백업하고
  manifest SHA에 포함한다.
- 같은 plan SHA 재실행은 새로 쌓인 성과를 다시 지우지 않는다.
- 장부 이관 직후 계좌 TWR·전략 A/B·나스닥/S&P500/코스피/코스닥의 첫 표본을
  모두 0%로 잡고 그 이후 1개월·3개월·전체를 일별 복리 누적한다.
- 기존 지수 가격 데이터 자체를 삭제하지 않는다. 다만 오염된 계좌 구간과
  비교할 수 없으므로 이전 성과 구간은 운영 차트에서 제외하고 forensic 백업으로
  보존한다.
- 장 시작 보유 동일가중 값은 `covered == eligible > 0`일 때만 표시한다.
  1/16처럼 부분수집이면 `자료 부족 1/16`으로 표시하고 지수와 비교하지 않는다.
- 화면 명칭을 `KIS 전체`가 아닌 `봇 운용자산 TWR`로 고쳐 사용자 수동보유·현금을
  포함한 전체 계좌 수익률로 오해하지 않게 했다.

특히 확인할 반례:

1. migration 중간 실패/부분 완료인데 성과 epoch가 먼저 바뀌는가.
2. 회계 완료 후 alpha 저장 직전 크래시와 재실행에서 성과가 반복 초기화되는가.
3. 손상 alpha 상태가 백업 없이 덮이는가.
4. 첫 계좌·지수 표본이 서로 다른 시각/기준으로 0이 되는가.
5. 1개월·3개월·전체에서 일별 수익률을 단순합산하거나 이중 복리하는가.
6. 일부 종목만 수집된 동일가중 값이 전체값으로 다시 노출되는가.
7. API에 금액·계좌번호·plan SHA 등 비공개 값이 새로 노출되는가.
8. 공개 사이트에 이전 오염 구간이 섞이거나 `-17%`가 그대로 이어지는가.
9. alpha import가 주문 API 또는 신규 KIS 호출을 추가하는가.

## 백테스트

개인 보유목록은 외부 시세 서비스로 보내지 않았다. Git에 이미 공개된 15종목,
공통 `now` 진입 22건에서 비교했다.

| 조합 | 총 손익 | 평균 보유 거래일 | 최대 이론 시드 점유 |
|---|---:|---:|---:|
| 10/20 | +1.082R | 14.27일 | 48.2% |
| 15/30 | +0.832R | 14.32일 | 48.2% |
| 20/40 | +0.582R | 14.32일 | 48.2% |

표본이 작고 차이가 AAPL 2건에 집중되어 10/20으로 최적화하지 않았다.
중간안 15/30을 `off` 기본값으로 구현하고 shadow 관찰을 요구한다.
상세는 `docs/STALL_EXIT_BACKTEST_2026-07-29.md`를 참고한다.

## 변경 파일

- `.gitignore`
- `bot/alpha.py`
- `bot/kis_exits.py`
- `bot/legacy_migration.py`
- `bot/settings.py`
- `bot/stall_exit.py`
- `config.py`
- `scanner/autopaper.py`
- `scanner/backtest.py`
- `scanner/site_app/app.js`
- `scanner/site_app/portfolio_math.js`
- `tests/site_math.test.js`
- `tests/test_alpha.py`
- `tests/test_legacy_migration.py`
- `tests/test_stall_exit.py`
- `tests/site_preview.py`
- `docs/STALL_EXIT_BACKTEST_2026-07-29.md`
- `docs/CODEX_HANDOFF.md`

## 실행 검증

- 전체 Python 테스트 모듈 `46/46` 통과
- Node 계산 테스트 `9/9` 통과
- `python -m compileall -q bot scanner tests`
- `node --check scanner/site_app/app.js`
- `node --check scanner/site_app/portfolio_math.js`
- `git diff --check`

## 운영 금지선

- 이 PR에서 Oracle 장부 apply를 하지 않는다.
- L1을 해제하지 않는다.
- `STALL_EXIT_MODE` 기본값은 `off`이며 `live`로 바꾸지 않는다.
- 승인 뒤 병합돼도 legacy 16건의 새 plan·사람 검토·backup/apply·KIS 수량 대조가
  먼저다.
- 그 다음 1–2주 `shadow`, 재검토, 별도 승인 뒤에만 `live`.
