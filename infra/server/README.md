# 상시 서버 패키지 (B-2) — Oracle Always-Free VM 등 $0 서버용

> KIS 미국주는 서버측 스톱이 없어 **파수꾼 상시 가동 = 손절 신뢰성**이다.
> 이 디렉터리는 그 서버에 올릴 파수꾼·매수 루프·조회 서비스와 watchdog을 담는다.
> KIS 개인계좌는 **IP allowlist가 없어** VM 재시작으로 IP가 바뀌어도 무방(토스와 다름).

## 구성
| 파일 | 역할 |
|---|---|
| `sentinel.service` | 파수꾼(매도) 상시 실행(`python -m bot.sentinel`, `SENTINEL_BROKER=kis`) |
| `buyloop.service` | 매수 루프 — autopaper 'now' 신호를 KIS에 미러 매수(`python -m bot.kis_buyloop --loop`) |
| `telegram.service` | 텔레그램 조회 봇(읽기전용) — `/보유`·`/종목 <코드>`(`python -m bot.kis_telegram`) |
| `portfolio-web.service` | 실제 KIS 보유종목·평단·현재가·손익을 보여주는 사설 웹 화면(`127.0.0.1:8888`) |
| `post-exit-refresh.timer` | 수익 매도 뒤 공개 일봉을 하루 2회 갱신하는 읽기전용 사후추적 |
| `watchdog.service` | heartbeat 감시 — 60s P0 · 90s 재기동(≤3회/10분) · 120s+ kill L1 |
| `watchdog.py` | 위 유닛이 실행하는 스크립트 |
| `autodeploy.sh` + `.service`/`.timer` | 자동 배포 — 5분마다 새 커밋 확인, 있으면 pull+재시작(스모크 실패 시 롤백) |

기본 장중 주기는 파수꾼 시세 20초, KIS 직접 잔고 폴백 60초, 브라우저 화면 5초,
매수 신호 확인 60초다. 브라우저는 파수꾼 공유 캐시만 읽어 KIS 호출을 추가하지
않는다. 각각 `SENTINEL_POLL_SECONDS`(5~60),
`PORTFOLIO_REFRESH_SECONDS`(5~300), `PORTFOLIO_BROWSER_REFRESH_SECONDS`(3~30),
`BUYLOOP_POLL_SECONDS`(10~300)로 조정한다. 너무 짧은 오설정은 코드에서 제한해
KIS 호출 폭주가 매매 안정성을 해치지 않게 한다.

> **손 = 매수(buyloop) + 매도(sentinel) 대칭.** 파수꾼만 켜면 손절만, 둘 다 켜면
> autopaper 결정을 KIS 모의계좌에 완전 미러(진입+청산). 처음엔 **매도만**(파수꾼)으로
> 검증하고, 안정되면 매수 루프를 켜는 걸 권장.

## 설치 순서 (Ubuntu 계열, Stage 1.5 모의 기준)
```bash
# 0) 사용자·디렉터리
sudo useradd -r -m bot || true
sudo mkdir -p /opt/stock /etc/stock
cd /opt/stock && sudo git clone https://github.com/easyseop/Stock-chart-analyze.git
sudo chown -R bot:bot /opt/stock

# 1) 시크릿 파일(값은 여기에만 — 깃·유닛 금지) — chmod 600!
sudo tee /etc/stock/kis.env >/dev/null <<'EOF'
KIS_ENV=mock
KIS_MOCK_APPKEY=여기에
KIS_MOCK_APPSECRET=여기에
KIS_MOCK_CANO=여기에
KIS_MOCK_ACNT_PRDT_CD=01
BOT_SEED_KRW=10000000
TRADE_STAGE=1.5
# ★ 매수·매도 루프가 같은 토큰 캐시 공유(I3: 발급 1분1회 — flock 직렬화)
KIS_TOKEN_CACHE=/opt/stock/kis_token.json
# BUY 체결은 확인됐지만 durable 회계가 3사이클 지연되면 운영자에게 1회 경보
KIS_ACCOUNTING_ALERT_CYCLES=3
# 주문·매수는 명시적으로만(기본 봉인):
# KIS_ORDERS_ENABLED=1     # ← 주문 전송 게이트(매도·매수 공통)
# ALLOW_BUY=1              # ← 매수 루프 게이트(이게 없으면 매수 시도 자체 봉인)
# ALLOWED_SYMBOLS=AAPL     # ← Stage 1.5/2 allowlist 필수(밖 종목 전부 거부)
TELEGRAM_BOT_TOKEN=여기에
TELEGRAM_CHAT_ID=여기에
NTFY_TOPIC=여기에
# 실제 매매·사용자 조회·치명 안전 경보만 전송(제안·성과·일상 운영 알림 억제)
NOTIFY_MODE=trade_only
EOF
sudo chmod 600 /etc/stock/kis.env

# 1.5) 무장(arming) — 사용자 기보유 심볼 denylist 캡처(매수 전 1회 필수)
sudo -u bot KIS_ENV=mock ... python3 /opt/stock/Stock-chart-analyze/scripts/kis_arm.py
#   (모의 새 계좌면 "빈 목록 — 깨끗한 계좌"가 정상. baseline 없으면 매수 전면 거부.)

# 2) 유닛 설치·기동 — 처음엔 매도(파수꾼)만, 안정 후 buyloop 추가
sudo cp /opt/stock/Stock-chart-analyze/infra/server/{sentinel,watchdog}.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel.service watchdog.service
# 매수도 켤 때(선택):
sudo cp /opt/stock/Stock-chart-analyze/infra/server/buyloop.service /etc/systemd/system/
sudo systemctl enable --now buyloop.service   # ALLOW_BUY=1·KIS_ORDERS_ENABLED=1 확인 후

# 텔레그램 조회 봇(선택, 읽기전용) — /보유·/종목 <코드>:
sudo cp /opt/stock/Stock-chart-analyze/infra/server/telegram.service /etc/systemd/system/
sudo systemctl enable --now telegram.service  # TELEGRAM_BOT_TOKEN·CHAT_ID 필요

# 실제 KIS 보유자산 웹 화면(선택, 읽기전용):
sudo install -o root -g root -m 644 \
  /opt/stock/Stock-chart-analyze/infra/server/portfolio-web.service \
  /etc/systemd/system/portfolio-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now portfolio-web.service

# 3) 확인
systemctl status sentinel watchdog buyloop telegram portfolio-web --no-pager
sudo -u bot python3 /opt/stock/Stock-chart-analyze/scripts/kis_preflight.py
```

익절 사후추적은 KIS 환경 파일을 받지 않는 별도 oneshot/timer다.

```bash
sudo cp infra/server/post-exit-refresh.service /etc/systemd/system/
sudo cp infra/server/post-exit-refresh.timer /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/post-exit-refresh.service.d
sudo cp infra/server/post-exit-refresh.oracle-ubuntu.conf \
  /etc/systemd/system/post-exit-refresh.service.d/oracle-ubuntu.conf
sudo systemctl daemon-reload
sudo systemctl enable --now post-exit-refresh.timer
sudo systemctl start post-exit-refresh.service
systemctl status post-exit-refresh.timer post-exit-refresh.service --no-pager
```

개인 웹의 `/api/post-exit.json`은 timer가 원자 발행한 JSON만 읽으며 브라우저
요청으로 일봉이나 KIS를 새로 조회하지 않는다.

> **텔레그램 조회 봇(`telegram.service`)** — 읽기전용. 매매 경로가 전혀 없어(조회
> API만) 토큰이 유출돼도 이 봇으로는 매매 불가. `getUpdates`는 이 프로세스만 쓴다
> (봇 하나에 `getUpdates` 소비자는 하나여야 함 — 중복 기동 금지, 409 Conflict).
> 알림 발송(notify)과는 무관: 발송은 sendMessage, 조회는 getUpdates로 분리됨.

### 내 KIS 보유자산 화면 접속

`portfolio-web`은 오라클 서버의 기존 `/etc/stock/kis.env`를 읽어 **해당 KIS
환경(`mock` 또는 `live`)의 실제 보유 잔고**를 조회한다. 보유수량·평단·잔고 응답
기준 현재가·평가금액·평가손익만 브라우저에 전달하며, 계좌번호·API 키·토큰과 주문
기능은 전달하지 않는다.

파수꾼은 장중 약 20초마다 이미 조회한 잔고·현재가를 0600 권한의 서버 내부 공유
파일에 원자적으로 저장한다. 브라우저는 이 값만 기본 5초마다 읽으므로 KIS 추가
호출은 0회다. 공유 캐시가 없거나 열린 시장에서 90초 이상 낡았을 때만
`portfolio-web`이 60초 캐시를 둔 직접 잔고 조회로 폴백한다. 따라서 화면을 여러 번
열어도 손절 주문의 KIS 유량과 경합하지 않는다.

보유 종목 상세에는 공유 캐시 기반 준실시간 선 차트와 기존 스캐너 캐시 기반 일봉
캔들·거래량·20/60/120일 이동평균을 표시한다. 성과 비교는 5분 간격으로 전략
A/B와 나스닥·S&P500·코스피·코스닥을 같은 0% 기준에서 기록한다.

서비스는 코드 수준에서 `127.0.0.1:8888`에만 바인딩한다. OCI 보안 목록이나 Ubuntu
방화벽에서 8888 포트를 열지 말고, 접속할 기기에서 SSH 터널을 연다.

```bash
ssh -N -L 8888:127.0.0.1:8888 <오라클-SSH-별칭>
# 터널을 켠 상태로 브라우저에서 http://127.0.0.1:8888/app/
```

```bash
# 서버 자체 점검(민감정보는 출력하지 않음)
curl -fsS http://127.0.0.1:8888/api/portfolio.json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["environment"], len(d["positions"]), d["partial"])'
```

자동배포에서 이 서비스도 함께 재시작하려면 `autodeploy.service`에 다음 환경값을
추가하고 sudoers의 고정 재시작 목록에도 `portfolio-web`을 포함한다.

```ini
Environment=AUTODEPLOY_SERVICES=sentinel buyloop telegram portfolio-web
```

## 모의 봇 켜기 — 최소 순서 (Stage 1.5)
1. `kis.env` 채우고 `chmod 600` → `kis_arm.py`로 무장(baseline 캡처).
2. **매도만 먼저**: `sentinel`+`watchdog` 기동. 손절 dry-run→실측 확인.
3. 매수 켜기: env에 `KIS_ORDERS_ENABLED=1`·`ALLOW_BUY=1`·`ALLOWED_SYMBOLS=…` 추가
   → `buyloop.service` 기동. `journalctl -u buyloop -f`로 게이트 통과/skip 관찰.
4. **롤아웃**: `TRADE_STAGE=1.5`(1종목·하루1건). 안정되면 `2.5`→`3`으로.
   비상시 `python -m bot.kill 1 "사유"`(kill-switch L1=신규매수 중지, 손절은 유지).

## 완전 미러 모드 (TRADE_STAGE=mirror, 모의 전용 — 사용자 지정 2026-07-15)
종목스크리너 페이퍼 시뮬(autopaper)을 **그대로 따라 사는** 모드. autopaper와
동일 캡: **동시 12종목 · 하루 신규 10건 · 거래당 risk 1% · allowlist 불필요**.
```bash
# kis.env에 (ALLOWED_SYMBOLS는 필요 없음 — 넣으면 그 목록만 사는 추가 펜스가 됨):
KIS_ORDERS_ENABLED=1
ALLOW_BUY=1
TRADE_STAGE=mirror
```
그대로 유지되는 방어선: kill-switch·부팅 대사·파수꾼 SLA·ownership(baseline
denylist)·원장(UNKNOWN 잠금·동일종목 in-flight·60s 간격)·사이징(SEED 분모·
총량 게이트=SEED 초과 투입 불가·매수여력 클램프)·세션(정규장만)·어닝 D-3 skip·
당일 매도 종목 재진입 쿨다운. 체결 확정은 잔고대사(ack→filled)가 자동 수행.

체결은 확인됐지만 브로커 체결가가 없어 costbook 반영이 늦어지면 원장 예약은
계속 유지돼 신규매수가 보수적으로 줄어든다. buyloop는 이 상태가 기본 3사이클
지속되면 치명 운영 알림을 한 번 보내며, 여러 건도 한 메시지로 요약한다. 회계가
끝나면 감시 상태를 자동 정리한다.
원장 flock은 비재귀이므로 락 보유 코드는 `_fold_unlocked`/`_append_unlocked`만
호출하고 잠금 순서는 `ledger > {costbook, kis_positions}`를 지킨다.

## L1 하향 전 읽기 전용 GO/NO-GO

`scripts/kis_l1_readiness.py`는 L1을 내리거나 주문을 보내지 않는다. 현재 서버의
원장·보호 포지션·원가장부·브로커 잔고·미체결·heartbeat와 별도 관찰 증거를
대조해 **운영자(operator)가 L1 하향을 검토할 수 있는지**만 판정한다. `GO`는
검토 가능, `NO-GO`는 L1 유지라는 뜻이다.

점검 범위는 두 가지다.

- `--scope l0`: KIS mock의 제한적 신규매수 재개용이다. 7일 shadow,
  Oracle 한·미 세션, GitHub 장애주입, 9종목 래칫, 동결 해제 결정은
  `INFO`로 표시한다. 대신 mock·L1·원장·미체결·수량·예산·heartbeat·
  fallback 0·stall shadow·동결 6종목 유지·mirror allowlist를 차단
  조건으로 사용한다.
- `--scope strict`(기본): 위 관찰 증거까지 모두 차단 조건으로 사용한다.

따라서 **2026-08-05까지 기다리는 조건은 제한적 L0에는 적용하지 않는다.**
정체청산 `live`, fallback 1, 동결 해제, 실전 전환에는 기존 조건이 계속
적용된다. Oracle 실행 순서와 rollback은
`docs/ORACLE_LIMITED_L0_RUNBOOK.md`를 따른다.

`strict`를 사용할 때는 먼저 예시 파일을 서버 상태 디렉터리로 복사하고 실제
관찰값을 기록한다.
예시의 빈 시각과 `pending`은 의도적인 차단값이다. 관찰하거나 결정하지 않은 값을
미리 통과값으로 바꾸지 않으며, API 키·계좌번호·토큰은 이 파일에 넣지 않는다.

```bash
sudo install -d -o ubuntu -g ubuntu -m 700 /var/lib/stock-l1-readiness
sudo install -o ubuntu -g ubuntu -m 600 \
  infra/server/l1-readiness-evidence.example.json \
  /var/lib/stock-l1-readiness/evidence.json
```

`strict`의 필수 증거:

- 정체청산 `shadow` 시작시각과 최소 7일 관찰
- 과거 절반익절 9종목의 `half_done=true` 및 `stop >= entry`
- close-only 동결 6종목별 `keep_close_only` 또는 `unfreeze_approved` 결정.
  `keep_close_only`이면 실제 동결 상태여야 하고, `unfreeze_approved`이면 별도
  승인 절차로 동결을 해제한 상태여야 한다.
- oracle-brain 한국·미국 각 1세션 이상 관찰
- GitHub 60분 장애주입과 그동안 신규주문 0건
- 위 관찰을 최근 72시간 안에 다시 확인한 `observed_at`

Oracle 저장소 루트에서 기존 KIS 환경을 source한 뒤 실행한다. `--broker`는
잔고와 미체결 **조회 API만** 호출하며 주문 모듈을 불러오지 않는다. 제한적
L0 점검은 evidence 파일이 없어도 실행할 수 있으며 별도 관찰은 `INFO`로 남는다.

```bash
# 표준 설치는 /etc/stock/kis.env. 현재 Oracle 운영 배치는
# docs/ORACLE_LIMITED_L0_RUNBOOK.md의 /home/ubuntu/kis.env를 사용한다.
set -a
. /etc/stock/kis.env
set +a
python3 scripts/kis_l1_readiness.py \
  --scope strict \
  --broker \
  --evidence /var/lib/stock-l1-readiness/evidence.json

# 제한적 mock 신규매수 점검
python3 scripts/kis_l1_readiness.py --scope l0 --broker --json
```

종료코드 `0`은 선택한 scope의 차단 게이트가 통과해 운영자 승인 검토가 가능하다는
뜻일 뿐이다. 실제 L1 하향은 사용자 별도 승인과 사유가 포함된
`python -m bot.kill 0 --lower ...` 절차가 여전히 필요하다. 하나라도 불명확하면
종료코드 `2`, `NO-GO — L1 유지`로 실패한다.

## 자동 배포 (autodeploy — 수동 pull/restart 대체)
5분마다 원격 브랜치를 확인해 **새 커밋이 있을 때만** `git pull`(fast-forward만)
→ 임포트 스모크 → 봇 재시작. 스모크 실패면 롤백하고 기존 코드로 계속(+P0 알림).
배포/실패 모두 텔레그램으로 알림. 끄기: `sudo systemctl disable --now autodeploy.timer`.
```bash
# ① 봇 계정이 재시작만 비번 없이 하도록(sudoers drop-in — 명령 고정이라 안전)
echo '<USER> ALL=(root) NOPASSWD: /usr/bin/systemctl restart sentinel buyloop telegram' \
  | sudo tee /etc/sudoers.d/stock-autodeploy && sudo chmod 440 /etc/sudoers.d/stock-autodeploy
# ② 유닛 설치(경로·User는 서버 배치에 맞게 수정) 후:
sudo systemctl daemon-reload && sudo systemctl enable --now autodeploy.timer
```
주의: env 파일(kis.env) 변경은 자동 배포 대상이 아니다(시크릿은 깃 밖) —
env를 바꾼 경우엔 여전히 수동 `systemctl restart` 1회 필요.

## 헬스 비콘 (원격 세션에서 서버 상태 확인용)
격리된 원격(웹) 세션은 이 서버에 SSH가 안 된다. 비콘이 5분마다 봇 가동상태를
ntfy 토픽에 올려두면, 원격에서 그 토픽을 HTTPS로 읽어 상태를 확인할 수 있다.
**민감정보(잔고·보유수량) 발행 안 함** — 운영 헬스만(가동여부·마지막 원장
이벤트·에러수). ntfy 토픽은 이름을 알면 읽히니 **추측 어려운 문자열**로.
```bash
# ① kis.env에 토픽 추가(추측 어려운 값). 예:
echo 'export NTFY_HEALTH_TOPIC=stock-health-a1d6048ee21e' | sudo tee -a /etc/stock/kis.env
# ② 유닛 설치(경로·User는 서버 배치에 맞게 수정) 후:
sudo systemctl daemon-reload && sudo systemctl enable --now health-beacon.timer
# ③ 즉시 1회 발행 확인:
sudo systemctl start health-beacon.service
```
발행 내용 읽기(원격/로컬 공통):
```bash
curl -s "https://ntfy.sh/<TOPIC>/json?poll=1" | tail   # 최근 캐시 메시지들
```
각 메시지의 `message` 필드가 헬스 JSON(`units`·`down`·`ledger_lines`·
`last_ledger`·`err_1h`). `down>0`이면 ntfy 알림도 high 우선순위로 뜬다.
끄기: `sudo systemctl disable --now health-beacon.timer`.

## 운영 규칙 (설계 04·REFLECTION 준수)
- **단일 프로세스 원칙(I3)**: 파수꾼 1개만 KIS 토큰을 쓴다. 루프를 늘리면 반드시
  같은 `KIS_TOKEN_CACHE`(flock)를 공유 — 토큰 발급 1분1회 제한 때문.
- **실전(Stage 2) 전환 시**: `/etc/stock/kis.env`만 교체(`KIS_ENV=live`+LIVE 키).
  단 **Stage 2 게이트 전엔 kis_orders가 live를 하드블록**한다 — 게이트 해제는
  Go/No-Go(REFLECTION §6) 통과 후 코드 변경으로만.
- **깃/CF에 live appsecret 금지(I4)** — 이 서버의 env 파일에만.
- **훈련(Stage 2 전 필수)**: `sudo reboot`(장중 재부팅 drill) 후 파수꾼이
  부팅 대사→감시 재개까지 자동 복구되는지, 네트워크 단절 drill에서 P0가 오는지.
- CF Worker dead-man은 바깥 계층(분 단위) — 초 단위 SLA는 이 watchdog이 담당.
