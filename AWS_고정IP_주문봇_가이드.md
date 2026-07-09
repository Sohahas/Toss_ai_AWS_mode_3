# AWS 고정 IP 주문봇 가이드

이 가이드는 집 PC를 24시간 켜두지 않고도 토스증권 자동매매가 계속 실행되도록 AWS 서버에서 주문봇을 돌리는 방법입니다.

## 추천 구성

최소요금과 쉬운 설정을 기준으로 아래 구성을 권장합니다.

```text
Render Web Dashboard   무료 Web Service
Render PostgreSQL      기존 DB 유지
AWS Lightsail Linux    주문봇 실행
AWS Static IP          토스증권 허용 IP 등록용
```

EC2도 가능하지만, 초보자에게는 Lightsail이 더 단순합니다. NAT Gateway, Load Balancer, RDS 추가 생성은 하지 마세요. 이번 구조에는 필요 없고 요금만 늘어납니다.

## 1. AWS 예산 알림 먼저 만들기

1. AWS 콘솔에 로그인합니다.
2. 상단 검색창에 `Budgets`를 입력합니다.
3. `Create budget`을 누릅니다.
4. 월 예산을 예: `10 USD`로 설정합니다.
5. 80%, 100% 도달 시 이메일 알림을 받도록 설정합니다.

## 2. Lightsail 서버 만들기

1. AWS 콘솔에서 `Lightsail`을 검색합니다.
2. `Create instance`를 누릅니다.
3. Region은 가능하면 `Asia Pacific (Seoul)`을 선택합니다.
4. Platform은 `Linux/Unix`를 선택합니다.
5. Blueprint는 `OS Only > Ubuntu`를 선택합니다.
6. 요금제는 가장 낮은 Linux public IPv4 번들을 선택합니다.
7. 이름은 예: `ai-stock-worker`로 정합니다.
8. 생성합니다.

## 3. Static IP 만들고 연결하기

1. Lightsail 왼쪽 메뉴에서 `Networking`으로 갑니다.
2. `Create static IP`를 누릅니다.
3. 방금 만든 인스턴스 `ai-stock-worker`에 연결합니다.
4. 표시되는 Static IP 주소를 복사합니다.
5. 토스증권 Open API 관리 화면에서 허용 IP를 이 Static IP로 변경합니다.

Static IP는 반드시 인스턴스에 연결해두세요. 연결하지 않고 방치한 IP는 비용과 관리 문제가 생길 수 있습니다.

## 4. Render DB 주소 복사

1. Render Dashboard에서 `ai-stock-assistant-db`를 엽니다.
2. `Connections` 또는 `Info` 메뉴에서 `External Database URL`을 복사합니다.
3. 주소가 `postgres://...` 또는 `?sslmode=require` 형태여도 프로그램이 자동 보정합니다.

## 5. AWS 서버 접속

Lightsail 인스턴스 화면에서 `Connect using SSH`를 누르면 브라우저 터미널이 열립니다.

서버에 접속한 뒤 아래 명령을 실행합니다.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip unzip
```

## 6. 프로젝트 업로드

초보자에게 가장 쉬운 방법은 GitHub에서 서버로 받는 방식입니다.

```bash
git clone https://github.com/본인계정/본인저장소.git
cd 저장소이름
```

만약 GitHub 저장소 안에 `ai-stock-assistant` 폴더가 한 단계 더 들어있다면 아래처럼 이동합니다.

```bash
cd ai-stock-assistant
```

## 7. 환경변수 작성

```bash
cp .env.aws.example .env
nano .env
```

아래 값은 반드시 실제 값으로 채워야 합니다.

```env
DATABASE_URL=Render External Database URL
TOSS_CLIENT_ID=토스 Client ID
TOSS_CLIENT_SECRET=토스 Client Secret
OPENAI_API_KEY=OpenAI API Key
TELEGRAM_BOT_TOKEN=Telegram Bot Token
TELEGRAM_CHAT_ID=Telegram Chat ID
```

주문봇 핵심 설정은 아래처럼 유지합니다.

```env
BROKER_MODE=toss
BROKER_API_ENABLED=true
LIVE_TRADING_ENABLED=true
MARKET_POLL_INTERVAL_SECONDS=300
ANALYSIS_INTERVAL_SECONDS=1800
EXTENDED_HOURS_ENABLED_BY_DEFAULT=false
EXTENDED_LIMIT_PRICE_BUFFER_PCT=0.005
US_DAY_MARKET_ENABLED=false
```

저장 후 `Ctrl + O`, `Enter`, `Ctrl + X`를 누릅니다.

## 8. 주문봇 설치

```bash
chmod +x setup_aws_worker.sh run_aws_worker.sh
./setup_aws_worker.sh
```

## 9. 주문봇 실행

```bash
./run_aws_worker.sh
```

아래와 비슷한 로그가 나오면 정상입니다.

```text
HTTP Request: POST https://openapi.tossinvest.com/oauth2/token "HTTP/1.1 200 OK"
HTTP Request: GET https://openapi.tossinvest.com/api/v1/holdings "HTTP/1.1 200 OK"
```

## 10. 24시간 자동 실행 등록

SSH 창을 닫아도 주문봇이 계속 돌게 하려면 systemd 서비스로 등록합니다.

먼저 현재 폴더 경로를 확인합니다.

```bash
pwd
```

그다음 서비스 파일을 만듭니다.

```bash
sudo nano /etc/systemd/system/ai-stock-worker.service
```

아래 내용을 붙여 넣습니다. `WorkingDirectory`는 `pwd`로 확인한 실제 경로로 바꾸세요.

```ini
[Unit]
Description=AI Stock Assistant AWS Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/저장소이름/ai-stock-assistant
ExecStart=/home/ubuntu/저장소이름/ai-stock-assistant/.venv/bin/python -m app.worker
Restart=always
RestartSec=10
EnvironmentFile=/home/ubuntu/저장소이름/ai-stock-assistant/.env

[Install]
WantedBy=multi-user.target
```

등록하고 실행합니다.

```bash
sudo systemctl daemon-reload
sudo systemctl enable ai-stock-worker
sudo systemctl start ai-stock-worker
sudo systemctl status ai-stock-worker
```

로그 확인:

```bash
journalctl -u ai-stock-worker -f
```

## 11. 대시보드에서 시작

1. Render 웹 대시보드에 접속합니다.
2. 보유 종목과 자산이 실제 계좌 기준으로 표시되는지 확인합니다.
3. `AI 행동패턴`에서 원하는 성향을 선택합니다.
4. `성향 저장`을 누릅니다.
5. 프리·애프터마켓 거래가 필요하면 `프리·애프터마켓 거래 허용`을 켭니다.
6. `자동매매 시작`을 누릅니다.

프리·애프터마켓 거래를 허용해도 시장가 주문은 사용하지 않습니다. 주문봇은 현재가 기준 기본 ±0.5% 이내의 지정가만 전송합니다.

## 문제 해결

### 토스 인증 실패 403: IP address not allowed

토스증권에 등록된 허용 IP가 AWS Static IP와 다릅니다.

- Lightsail Static IP를 다시 확인합니다.
- 토스 Open API 허용 IP를 AWS Static IP로 변경합니다.
- 주문봇을 재시작합니다.

```bash
sudo systemctl restart ai-stock-worker
```

### 대시보드가 계좌를 못 읽음

- AWS 주문봇이 실행 중인지 확인합니다.
- `.env`의 `DATABASE_URL`이 Render External Database URL인지 확인합니다.
- Render Web과 AWS Worker가 같은 DB를 바라보는지 확인합니다.

### 비용이 걱정됨

- Lightsail 인스턴스 1개만 사용합니다.
- Static IP는 1개만 만들고 반드시 인스턴스에 연결합니다.
- NAT Gateway, Load Balancer, RDS는 만들지 않습니다.
- AWS Budgets 알림을 반드시 켭니다.
