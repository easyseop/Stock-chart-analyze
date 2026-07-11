# 상시 서버 패키지 (B-2) — Oracle Always-Free VM 등 $0 서버용

> KIS 미국주는 서버측 스톱이 없어 **파수꾼 상시 가동 = 손절 신뢰성**이다.
> 이 디렉터리는 그 서버에 올릴 전부다: systemd 유닛 2개 + watchdog.
> KIS 개인계좌는 **IP allowlist가 없어** VM 재시작으로 IP가 바뀌어도 무방(토스와 다름).

## 구성
| 파일 | 역할 |
|---|---|
| `sentinel.service` | 파수꾼 상시 실행(`python -m bot.sentinel`, `SENTINEL_BROKER=kis`) |
| `watchdog.service` | heartbeat 감시 — 60s P0 · 90s 재기동(≤3회/10분) · 120s+ kill L1 |
| `watchdog.py` | 위 유닛이 실행하는 스크립트 |

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
# 주문·매수는 명시적으로만(기본 봉인):
# KIS_ORDERS_ENABLED=1
# ALLOW_BUY=1
# ALLOWED_SYMBOLS=AAPL
TELEGRAM_BOT_TOKEN=여기에
TELEGRAM_CHAT_ID=여기에
NTFY_TOPIC=여기에
EOF
sudo chmod 600 /etc/stock/kis.env

# 2) 유닛 설치·기동
sudo cp /opt/stock/Stock-chart-analyze/infra/server/{sentinel,watchdog}.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel.service watchdog.service

# 3) 확인
systemctl status sentinel watchdog --no-pager
sudo -u bot python3 /opt/stock/Stock-chart-analyze/scripts/kis_preflight.py
```

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
