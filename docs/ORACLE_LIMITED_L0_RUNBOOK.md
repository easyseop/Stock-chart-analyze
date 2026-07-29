# Oracle 제한적 L0 전환 런북

## 1. 목적과 현재 상태

이 런북은 KIS **모의계좌**에서 일반 GitHub 신호의 신규매수만 제한적으로
재개할 때 사용한다. L0는 신규매수를 허용하는 kill-switch 레벨이다.

- 저장소: `easyseop/Stock-chart-analyze`
- 작업 브랜치: `codex/l1-readiness-audit`
- 대상 기본 브랜치: `claude/happy-gauss-cwoq21`
- Draft PR: #97 `Add read-only L1 readiness audit`
- 현재 상태: PR 미병합, Oracle 미배포, L1 유지

**2026-08-05까지 기다릴 필요는 없다.** 단, 이것은 아래 제한적 L0 범위에만
해당한다. 정체청산 `live`, Oracle fallback 1, 동결 6종목 해제, 실전계좌
전환은 각각 기존 관찰과 별도 사용자 승인이 필요하다.

## 2. 제한적 L0에서 유지할 경계

- `KIS_ENV=mock`
- `STALL_EXIT_MODE=shadow`
- `ORACLE_SIGNAL_FALLBACK_ENABLED=0`
- AQN, CAG, GPK, LW, SNN, VRSK close-only 동결
- `TRADE_STAGE=mirror`
- 비어 있지 않은 `ALLOWED_SYMBOLS`
- `ALLOW_BUY=1`, `KIS_ORDERS_ENABLED=1`
- 실제 L0 전환 전까지 kill-switch L1

`ALLOWED_SYMBOLS`는 Git이 정할 수 없는 사람의 결정이다. 실제로 재매수를
허용할 종목만 쉼표로 구분해 기록한다. 동결 6종목을 목록에 넣어도 ownership
게이트가 차단하지만, 운영 의도를 명확히 하려면 목록에서 제외한다.

## 3. Oracle에서 실행할 순서

### 3.1 PR 병합과 코드 배포

PR #97의 CI가 성공한 것을 확인하고 기본 브랜치에 병합한다. Oracle에서는
서버가 추적하는 기본 브랜치에서만 fast-forward pull한다. feature branch를
직접 운영 브랜치로 사용하지 않는다.

```bash
cd /home/ubuntu/Stock-chart-analyze
git status --short --branch
git pull --ff-only
git log -5 --oneline
.venv/bin/python scripts/kis_l1_readiness.py --help | grep -q -- "--scope"
```

작업트리가 clean이 아니거나 pull이 fast-forward로 끝나지 않으면 중단한다.
현재 브랜치가 `claude/happy-gauss-cwoq21`이 아니어도 중단한다.
마지막 명령이 종료코드 0이 아니면 PR #97의 scope 변경이 없는 것이므로 중단한다.

### 3.2 환경 설정

`/home/ubuntu/kis.env`의 비밀값은 출력하거나 Git에 저장하지 않는다. 다음 안전
설정만 직접 확인·수정한다.

```dotenv
KIS_ENV=mock
KIS_ORDERS_ENABLED=1
ALLOW_BUY=1
TRADE_STAGE=mirror
ALLOWED_SYMBOLS=<사용자가 승인한 종목 목록>
ORACLE_SIGNAL_FALLBACK_ENABLED=0
```

sentinel의 systemd drop-in은 `STALL_EXIT_MODE=shadow`를 계속 사용한다.
환경 변경 후에도 L1 상태를 먼저 확인하고 관련 서비스를 재시작한다.

```bash
cd /home/ubuntu/Stock-chart-analyze
sudo -u ubuntu bash -lc '
  cd /home/ubuntu/Stock-chart-analyze
  set -a
  . /home/ubuntu/kis.env
  set +a
  .venv/bin/python -m bot.kill
'
sudo systemctl restart sentinel buyloop watchdog
sudo systemctl is-active sentinel buyloop watchdog
```

출력이 `L1`, 서비스 세 개가 모두 `active`가 아니면 중단한다.

### 3.3 실행 중 프로세스의 안전 설정 확인

다음 명령은 프로세스 환경에서 안전 관련 키만 출력한다. API key, 계좌번호,
토큰은 출력하지 않는다.

```bash
sudo sh -c '
for unit in sentinel.service buyloop.service; do
  pid=$(systemctl show "$unit" -p MainPID --value)
  echo "[$unit pid=$pid]"
  test "$pid" -gt 0 || continue
  tr "\0" "\n" < "/proc/$pid/environ" |
    grep -E "^(KIS_ENV|KIS_ORDERS_ENABLED|ALLOW_BUY|TRADE_STAGE|ALLOWED_SYMBOLS|ORACLE_SIGNAL_FALLBACK_ENABLED|STALL_EXIT_MODE)=" |
    sort
done
'
```

sentinel은 `KIS_ENV=mock`, `STALL_EXIT_MODE=shadow`여야 한다. buyloop는
`KIS_ENV=mock`, fallback 0, mirror, 비어 있지 않은 allowlist, 두 주문
플래그 1이어야 한다. 누락이나 불일치가 있으면 L1을 유지하고 unit/env부터
수정한다.

### 3.4 읽기 전용 L0 점검

아래 명령은 브로커 잔고와 미체결을 조회하지만 주문을 보내거나 L1을 바꾸지
않는다. service 설정 확인과 동일한 안전값으로 평가기를 실행한다.

```bash
sudo -u ubuntu bash -lc '
  cd /home/ubuntu/Stock-chart-analyze
  set -a
  . /home/ubuntu/kis.env
  set +a
  export STALL_EXIT_MODE=shadow
  export ORACLE_SIGNAL_FALLBACK_ENABLED=0
  .venv/bin/python scripts/kis_l1_readiness.py --scope l0 --broker --json
'
```

다음 조건을 모두 만족해야 사용자 승인 단계로 진행한다.

- 종료코드 0
- `"scope": "l0"`
- `"ready_for_operator_review": true`
- `"blockers": []`
- `context.allowed_symbols`가 승인할 목록과 정확히 일치
- `context.position_counts_by_sleeve`의 A/B 개수를 사람이 확인

`informational_findings`에 7일 shadow, Oracle 세션, 장애주입 등이 남아 있어도
제한적 L0에는 차단이 아니다. 해당 결과를 완료로 바꿔 쓰지 않는다.

## 4. 사용자 승인과 L0 전환

여기부터는 자동 진행하지 않는다. 점검 JSON과 A/B 개수, allowlist를 사용자에게
보여주고 다음 범위를 명시적으로 승인받는다.

> KIS mock 계좌의 제한적 L0 신규매수를 승인한다. `ALLOWED_SYMBOLS` 펜스,
> 정체청산 shadow, Oracle fallback 0, 동결 6종목, 실전 하드블록을 유지한다.
> A 포지션 수가 mirror 상한 12개 미만으로 줄면 A 신규매수도 열릴 수 있음을
> 인지한다. stall live, fallback 1, 동결 해제, 실전 전환은 승인하지 않는다.

승인 후 Oracle 운영자가 실제 승인 내용을 ack 문자열에 넣어 한 번만 실행한다.

```bash
sudo -u ubuntu bash -lc '
  cd /home/ubuntu/Stock-chart-analyze
  set -a
  . /home/ubuntu/kis.env
  set +a
  .venv/bin/python -m bot.kill 0 \
    "사용자 승인: mock 제한적 L0, allowlist 확인" --lower
'
```

출력이 `L0`인지 확인한다. 정규장에 fresh 신호, 가격 범위, allowlist, 포지션
한도, 예산, 매수 가능 현금 조건을 모두 만족하는 종목이 있으면 기본 60초
매수루프 안에 주문을 시도한다. 현금이 없거나 계산 수량이 0이면 주문하지 않는다.
조건을 만족하는 종목이 없으면 L0여도 신규주문은 0건이다.
`KILL_LEVEL` 환경값이 L1 이상을 강제하면 하향 명령은 거부된다. 이 경우 값을
자동으로 지우지 말고 실제 unit 설정과 운영자 의도를 다시 확인한다.

## 5. 첫 주문 관찰과 rollback

```bash
sudo journalctl -u buyloop -f
```

첫 주문 1건에서 submit→ack/filled→accounted, `kis_positions` 보호선 생성,
텔레그램 체결 알림을 확인한다. 다음 중 하나가 발생하면 즉시 L1로 올린다.

- `UNKNOWN` 또는 원장 손상
- 브로커·보호원장·costbook 수량 불일치
- sentinel heartbeat 120초 초과
- 예산·현금·수량 계산 이상
- allowlist 밖 주문 또는 동결종목 주문 시도

```bash
cd /home/ubuntu/Stock-chart-analyze
sudo -u ubuntu bash -lc '
  cd /home/ubuntu/Stock-chart-analyze
  set -a
  . /home/ubuntu/kis.env
  set +a
  .venv/bin/python -m bot.kill 1 "제한적 L0 이상 감지 — 신규매수 즉시 중지"
'
```

L1은 신규매수만 중지하며 기존 보호매도는 유지한다. 원인을 확인하기 전 다시
L0으로 내리지 않는다.

## 6. 완료와 미완료의 경계

이 저장소에서 완료 가능한 것은 PR 코드·테스트·런북·원격 push까지다.
Oracle 접속이 필요한 PR 병합 후 배포, 환경값 확인, 브로커 조회, 사용자 승인,
L0 전환, 첫 주문 관찰은 환경 작업으로 남는다.
