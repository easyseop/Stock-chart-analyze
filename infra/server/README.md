# 상시 서버 패키지 (B-2) — Oracle Always-Free VM 등 $0 서버용

> KIS 미국주는 서버측 스톱이 없어 **파수꾼 상시 가동 = 손절 신뢰성**이다.
> 이 디렉터리는 그 서버에 올릴 전부다: systemd 유닛 2개 + watchdog.
> KIS 개인계좌는 **IP allowlist가 없어** VM 재시작으로 IP가 바뀌어도 무방(토스와 다름).

## 구성
| 파일 | 역할 |
|---|---|
| `sentinel.service` | 파수꾼(매도) 상시 실행(`python -m bot.sentinel`, `SENTINEL_BROKER=kis`) |
| `buyloop.service` | 매수 루프 — autopaper 'now' 신호를 KIS에 미러 매수(`python -m bot.kis_buyloop --loop`) |
| `telegram.service` | 텔레그램 조회 봇(읽기전용) — `/보유`·`/종목 <코드>`(`python -m bot.kis_telegram`) |
| `watchdog.service` | heartbeat 감시 — 60s P0 · 90s 재기동(≤3회/10분) · 120s+ kill L1 |
| `watchdog.py` | 위 유닛이 실행하는 스크립트 |
| `autodeploy.sh` + `.service`/`.timer` | 자동 배포 — 5분마다 새 커밋 확인, 있으면 pull+재시작(스모크 실패 시 롤백) |

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
# 주문·매수는 명시적으로만(기본 봉인):
# KIS_ORDERS_ENABLED=1     # ← 주문 전송 게이트(매도·매수 공통)
# ALLOW_BUY=1              # ← 매수 루프 게이트(이게 없으면 매수 시도 자체 봉인)
# ALLOWED_SYMBOLS=AAPL     # ← Stage 1.5/2 allowlist 필수(밖 종목 전부 거부)
TELEGRAM_BOT_TOKEN=여기에
TELEGRAM_CHAT_ID=여기에
NTFY_TOPIC=여기에
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

# 3) 확인
systemctl status sentinel watchdog buyloop telegram --no-pager
sudo -u bot python3 /opt/stock/Stock-chart-analyze/scripts/kis_preflight.py
```

> **텔레그램 조회 봇(`telegram.service`)** — 읽기전용. 매매 경로가 전혀 없어(조회
> API만) 토큰이 유출돼도 이 봇으로는 매매 불가. `getUpdates`는 이 프로세스만 쓴다
> (봇 하나에 `getUpdates` 소비자는 하나여야 함 — 중복 기동 금지, 409 Conflict).
> 알림 발송(notify)과는 무관: 발송은 sendMessage, 조회는 getUpdates로 분리됨.

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
