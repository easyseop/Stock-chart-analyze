# Claude 적대 검토 요청 — CVNA BUY 유실 + 절반익절 2건 복구

검토 브랜치: `codex/cvna-partial-exit-recovery`  
기준: `claude/happy-gauss-cwoq21 @ a5ced2f`  
핵심 구현 체크포인트: `5c7cb79`  
사실 조사: `docs/CVNA_PARTIAL_EXIT_FORENSICS_2026-08-20.md`

## 전제

옛 BUY 74주 전용 plan은 절대 apply하지 않는다. 2026-08-20 파수꾼이 CVNA
37주를 69.51에 절반익절해 현재 수량은 37주다. Oracle 원문 조사 결과 SELL 회계는
원가 없이 proceeds만 잡힌 것이 아니다. SELL 대사가 74주 legacy lot을 자동 생성한
뒤 37주를 정상 close해 잔여 원가·실현손익이 이미 정확하다.

이 변경은 costbook을 다시 쓰지 않고, 누락된 BUY 원장·남은 포지션 정체성·거래이력
증명만 잇는다. 병합·배포·운영 apply는 모두 사용자 별도 승인 사항이다.

## 우선 반증 질문

1. raw costbook에 add/close 중 하나가 없거나 event_id가 중복·왜곡됐는데 plan이
   생성되는 경로가 있는가?
2. 지시서 정수 원가와 durable 원가의 0.6원 차이를 허용한 `<1원` 경계가 다른
   경제 오염을 세탁할 수 있는가?
3. BUY 74, SELL 37, 현재 37 중 어느 하나가 바뀌어도 plan/apply가 mutation 전에
   거부되는가? 거래소별 체결 중복은 동일 증거로 정규화되고 충돌은 거부되는가?
4. SELL order가 filled/accounted 37이 아니거나 pos_key가 costbook key와 다르면
   복구가 진행되는가?
5. apply가 기존 legacy add/close를 다시 기록하거나 새 BUY lot을 추가해 open cost,
   buy_cost, sell_proceeds, daily PnL을 바꾸는 경로가 있는가?
6. BUY reconcile 뒤, position repair 전, accounted 전, complete meta 전의 각 크래시
   창에서 재실행이 37주를 74주로 부활시키거나 SELL을 재적용하는가?
7. `accounting_repair`가 기존 half_done을 지워 파수꾼이 또 37주를 절반익절하거나,
   stop 65.03을 stop0 60.48로 낮추는가?
8. recovery pending 예약이 BUY 복구 완료 전까지 유지되고 완료 뒤 해제되는가?
9. plan 뒤 새 부분매도/전량매도/추가매수가 발생하면 fresh KIS 잔고 재검사가
   백업·원장 mutation보다 먼저 거부하는가?
10. 서비스 stop 인자만 위조하고 systemd/mask/heartbeat/수동 프로세스 게이트를
    우회할 수 있는가? 백업 뒤 프로세스 재등장도 잡는가?
11. 완료 뒤 같은 plan을 재실행할 때 backup 생성 또는 원장 append가 생기는가?
12. 거래이력의 BUY·SELL verified 승격이 BUY order, SELL order, seed add, close 네
    증거 중 하나가 없어도 일어나는가? 내부 key·ODNO가 API로 노출되는가?
13. 기존 v1 단일 BUY 복구, 일반 buy_fill/sell_fill, legacy migration, 거래이력의
    key 없는 복수 lot fail-closed 계약이 퇴행했는가?
14. 이 모듈의 import graph 또는 CLI에서 주문·cancel·kill·환경 변경 경로가
    새로 생겼는가?

## 실행 요청

- `python -m tests.test_accounting_recovery`
- `python -m tests.test_trade_history`
- `python -m tests.run_all` (번들 의존성 환경)
- `node tests/site_math.test.js`
- `python -m compileall -q bot tests`
- `git diff --check`

코덱스가 보고한 mutation 4종을 독립 재주입하고, 추가로 아래를 권한다.

- `<1원`을 `<=1원` 또는 큰 허용치로 바꾸기
- costbook 원문 event_id 중복 거부 제거
- `_validate_sell_order` accounted 검사 제거
- `repair_buy_fill`의 절대수량을 74로 바꾸기
- `economic_seed_event_id`/`economic_sell_event_id` 중 하나 없이 거래이력 승격
- apply의 두 번째 broker recheck 제거

P0~P3로 판정하고 P0/P1이 하나라도 있으면 병합 차단해 달라. P0/P1=0이어도
운영 apply 승인은 별도다.
