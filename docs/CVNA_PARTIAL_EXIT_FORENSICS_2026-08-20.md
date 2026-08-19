# CVNA 유실 BUY + 절반익절 forensic 조사

작성: Codex, 2026-08-20  
성격: Oracle 읽기 전용 실측 + 복구 설계 기록  
금지: 이 문서는 운영 원장 apply 승인서가 아니다.

## 1. 결론

기존 74주 BUY 전용 plan이 `보호 포지션 수량 불일치`로 거부된 것은 정상이다.
plan 생성 전에 파수꾼이 37주를 절반익절해 현재 수량이 37주로 바뀌었기 때문이다.
옛 plan은 폐기해야 하며 74주를 다시 원가장부에 넣으면 이중계상이 된다.

SELL 37주는 원가 없이 proceeds만 잡히지 않았다. `bot.kis_accounting.sync_fill`의
기존 legacy 보호 경로가 매도 직전에 저장된 평단 65.03과 당시 수량 74를 읽어
74주 lot을 먼저 만든 뒤, 그 lot에서 37주를 차감했다. 지시서의 (a) 현금 부풀림과
(b) 무원가 fail-closed 어느 쪽도 아니며, 제3의 경로인 **legacy seed 후 정상
부분청산**이었다.

경제 장부는 이미 맞다. 남은 결함은 최초 BUY 주문이 `rejected/filled=0/accounted=0`
으로 남고, 보호 포지션의 `pos_key`가 비어 있으며, 거래이력에서 BUY가 누락되고
SELL이 미확정으로 보이는 소유권 연결 문제다.

## 2. Oracle 읽기 전용 실측

- Oracle 코드 HEAD: `a5ced2f90f0df4d5474056afb54065e994ad1b99`
- sentinel, buyloop, watchdog, telegram, portfolio-web: 모두 active
- 조사 중 서비스 정지·환경 변경·원장 쓰기·주문 호출: 0건
- KIS 환경: mock

### 주문 원장

| 구분 | 값 |
|---|---|
| BUY key | `kb:CVNA:CVNA-2026-08-18-now` |
| BUY ODNO | `0000040445` |
| BUY 사실 | 74주, pullback, 65.03 체결 |
| BUY 현 원장 | rejected, filled 0, accounted 0 |
| SELL key | `xe:CVNA:half:#1` |
| SELL ODNO | `0000041614` |
| SELL 사실 | 37주, 69.51 체결 |
| SELL 현 원장 | filled 37, accounted 37 |

### 보호 포지션 원장

- 최초 임시 open: 74주, entry 65.03, stop 60.48
- `sell_fill`: 37주, event `fill:xe:CVNA:half:#1:SELL:37`
- `half_done`: 기록됨
- `raise`: stop 65.03으로 본전 래칫
- 현재 fold: 37주, entry 65.03, stop 65.03, stop0 60.48, half_done=true
- 현재 `pos_key`: 빈 문자열

### costbook 원문과 fold

SELL 대사 직전에 다음 add가 자동 생성됐다.

- event: `fill:xe:CVNA:half:#1:SELL:37:legacy`
- key: `legacy:A:CVNA:?`
- qty: 74
- fill_price: 65.03
- fx: 1380
- cost: 6,640,863.600000001원

그 다음 동일 key에서 37주가 정상 close됐다.

- event: `fill:xe:CVNA:half:#1:SELL:37`
- proceeds: 3,549,180.6000000006원
- cost closed: 3,320,431.8000000003원
- realized PnL: +228,748.80000000028원
- day_kst: 2026-08-20

현재 열린 lot은 `legacy:A:CVNA:?` 하나뿐이다.

- qty: 37
- remaining cost: 3,320,431.8000000003원
- 정수 표시: 잔여 원가 3,320,432원, 실현손익 +228,749원

지시서의 원가 6,640,863원과 계산 원가 6,640,863.6원의 차이는 0.6원이다.
새 복구는 이미 durable한 계산 원가를 재작성하지 않고, 지시서 정수와 1원 미만인지
검증해 반올림 차이로만 기록한다.

### KIS fresh 재확인

- BUY ODNO: 74주 @ 65.03
- SELL ODNO: 37주 @ 69.51
- 현재 잔고: 37주 @ 65.03
- 조회 실패·거래소별 증거 충돌: 없음

## 3. 코드 원인

`bot/kis_accounting.py`의 SELL 회계는 costbook 수량이 매도 수량보다 적을 때
`kis_positions`의 legacy 수량·평단을 사용해 부족 lot을 먼저 시딩한다. 이후
proceeds를 계산해 `close_lot`, `apply_sell_fill`, `accounted` 순서로 확정한다.

이 방어 덕분에 0원가 이익은 발생하지 않았다. 반면 최초 BUY order key와 무관한
SELL key 기반 legacy event가 경제 장부의 정체성이 됐으므로, BUY 원장과 거래이력
연결은 자동으로 회복되지 않았다.

## 4. 구현한 복구 방식

`bot.accounting_recovery plan-partial-exit` v2는 다음을 한 시나리오로 묶는다.

1. BUY 74주 체결 증거
2. SELL 37주 체결 증거
3. 현재 KIS 37주 잔고
4. SELL 원장의 filled/accounted 37
5. costbook legacy add 74 + close 37 원문 이벤트
6. 현재 열린 lot 37와 보호 포지션 37·half_done·본전 래칫

apply는 위 증거를 다시 확인하고 다음만 append한다.

- BUY 주문 원장: filled 74, accounted 74, recovery complete
- BUY의 pos_key: 이미 경제 장부가 사용하는 `legacy:A:CVNA:?`로 연결
- 보호 포지션: 절대 잔여 37주, stop 65.03, stop0 60.48, half_done 유지
- 거래이력: 증명된 BUY 74와 SELL 37을 verified로 연결

costbook add/close는 **0건**이다. 테스트는 apply 전후 costbook 파일이 바이트 단위로
같음을 단언한다.

## 5. 안전 계약

- 주문 모듈 import·주문 전송: 0
- 5분 plan + canonical SHA256 + exact operator ack
- sentinel/buyloop 정지·유효한 mask 또는 아래 disable 대체 계약·heartbeat
  stale·수동 프로세스 0
- apply 전·백업 후 KIS BUY/SELL/잔고 재조회
- 주문/포지션/costbook 절대경로 일치
- 미존재 전용 백업 디렉터리
- 기존 economic event 원문 중복·왜곡·누락 시 거부
- plan 뒤 잔고가 37에서 달라지면 백업·mutation 전에 거부
- 중간 크래시 재실행 시 costbook·SELL 무변조, BUY/포지션만 멱등 완료
- 이미 완료된 같은 plan 재실행은 세 원장 바이트 무변경

## 6. 검증

- focused accounting recovery: 8/8 PASS
- trade history: 4/4 PASS
- Python 전체: 69/69 모듈 PASS
- Node 웹 계산: 19/19 PASS
- compileall: PASS
- `git diff --check`: PASS

체크포인트 커밋 뒤 다음 방어를 독립 제거했고 모두 테스트 exit 1로 KILLED됐다.

1. costbook 재기입 주입 → `RecoveryRefused: CVNA 잔여 lot 수량/원가 불일치`
2. half_done 전달 제거 → `RecoveryRefused: 보호 포지션 ... 절반익절 상태 불일치`
3. apply 시 fresh 잔고 재조회 제거 → `AssertionError: plan 뒤 잔고 변화를 apply가 무시`
4. SELL verified 연결 제거 → 거래이력 verified assertion 실패

## 7. 다음 단계

1. Claude 적대 검토에서 P0/P1=0 확인
2. 사용자 병합 승인
3. Oracle 코드 배포(L1·mock·기존 설정 유지)
4. 장외에 아래 런북으로 서비스를 quiesce한 뒤 새 5분 v2 plan 생성
5. BUY/SELL/잔고/costbook 표를 사람이 다시 확인
6. exact SHA ack에 대한 사용자 별도 apply 승인
7. apply 후 KIS 37 = kpos 37 = costbook 37, stop/half_done, 거래이력 검증
8. 서비스 복구

그 전에는 옛 74주 전용 plan 또는 새 v2 plan을 운영 원장에 apply하지 않는다.

### Oracle `/etc` 실파일 유닛용 quiesce 런북

Oracle의 `sentinel.service`·`buyloop.service`는 `/etc/systemd/system` 실파일이라
`mask --runtime`이 `/run`에 만든 마스크보다 우선한다. 이 배치에서는 runtime
mask를 사용하지 않고 자동 재기동 주체를 먼저 멈춘 뒤 주문 유닛을 disable한다.

```bash
# 1) 새 배포·watchdog 재기동 경로부터 차단
sudo systemctl stop autodeploy.timer autodeploy.service watchdog.service

# 2) 주문 프로세스를 멈추고 재부팅 자동기동도 임시 차단
sudo systemctl stop sentinel.service buyloop.service
sudo systemctl disable sentinel.service buyloop.service

# 3) 도구와 사람이 같은 계약을 확인
systemctl is-active sentinel.service buyloop.service \
  watchdog.service autodeploy.timer autodeploy.service
systemctl is-enabled sentinel.service buyloop.service
pgrep -af 'bot\.(sentinel|kis_buyloop)' || true
```

모든 `is-active` 결과가 `inactive`, 두 `is-enabled` 결과가 `disabled`, 수동
프로세스가 0이고 sentinel heartbeat가 120초보다 오래된 뒤에만 새 plan/apply를
진행한다. 하나라도 다르면 apply하지 않는다.

apply가 성공하거나 중단된 뒤에는 아래 순서로 원래 상시 운영을 복구한다.

```bash
sudo systemctl enable sentinel.service buyloop.service
sudo systemctl start sentinel.service buyloop.service
sudo systemctl start watchdog.service autodeploy.timer
systemctl is-active sentinel.service buyloop.service watchdog.service \
  autodeploy.timer
```

서비스 복구 뒤 heartbeat·보호 SELL·buyloop 게이트를 확인한다. apply 실패 여부와
관계없이 자동 재기동 주체를 다시 켜는 단계까지가 한 런북이다.
