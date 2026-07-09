# PC 주문봇 실행 가이드

이 버전은 역할을 둘로 나눕니다.

- Render: 웹 대시보드와 PostgreSQL DB만 담당
- 내 PC: 토스증권 API 조회, 계좌 갱신, AI 판단, 실제 매수·매도 주문, Telegram 알림 담당

즉, 토스증권에 허용된 공인 IP가 내 PC라면 Render 서버 IP를 토스에 등록하지 않아도 됩니다.

## 1. Render에서 확인할 것

Render에는 다음 2개만 살아 있으면 됩니다.

```text
ai-stock-assistant-web       Web Dashboard
ai-stock-assistant-db        PostgreSQL
```

기존에 `ai-stock-assistant-worker`가 있다면 Render에서 중지하거나 삭제하세요. 이제 실제 주문 Worker는 PC에서 실행합니다.

## 2. Render Web 환경변수

Render Web 서비스는 토스 API를 직접 호출하면 안 됩니다. 아래처럼 맞춥니다.

```env
BROKER_MODE=toss
BROKER_API_ENABLED=false
LIVE_TRADING_ENABLED=true
MARKET_POLL_INTERVAL_SECONDS=300
ANALYSIS_INTERVAL_SECONDS=1800
```

Render Web에는 토스 Client Secret, OpenAI Key, Telegram Token을 넣지 않아도 됩니다. Web은 DB를 읽고 버튼 명령을 저장하는 역할만 합니다.

## 3. Render DB 주소 복사

1. Render Dashboard에서 `ai-stock-assistant-db`를 엽니다.
2. `Connections` 또는 `Info` 메뉴에서 `External Database URL`을 복사합니다.
3. 이 주소를 PC의 `.env` 파일 `DATABASE_URL`에 넣습니다.

주소가 `postgres://...` 또는 `?sslmode=require` 형태여도 프로그램이 자동 보정합니다. 그래도 직접 적는다면 아래 형태가 가장 안전합니다.

```env
DATABASE_URL=postgresql+asyncpg://아이디:비밀번호@호스트/DB이름?ssl=true
```

## 4. PC 주문봇 설정

1. 프로젝트 폴더에서 `.env.pc.example` 파일을 복사합니다.
2. 복사본 이름을 `.env`로 바꿉니다.
3. `.env` 안의 값을 채웁니다.

반드시 아래 값은 PC에서만 `true`여야 합니다.

```env
BROKER_MODE=toss
BROKER_API_ENABLED=true
LIVE_TRADING_ENABLED=true
```

## 5. PC 주문봇 설치

프로젝트 폴더에서 `setup_pc_worker.bat`을 더블클릭합니다.

설치가 끝나면 창을 닫아도 됩니다.

## 6. PC 주문봇 실행

`run_pc_worker.bat`을 더블클릭합니다.

검은 창이 계속 떠 있어야 정상입니다. 이 창이 떠 있는 동안 PC가 토스증권 API를 호출하고, Render DB를 갱신하며, 실제 주문도 PC에서 실행합니다.

## 7. Dashboard에서 시작

1. Render Web Dashboard에 접속합니다.
2. 우측 상단이 `토스증권 실계좌 · PC 주문봇이 실제 주문`으로 보이는지 확인합니다.
3. `보유 종목`과 자산이 실제 계좌 기준으로 갱신되는지 확인합니다.
4. `자동매매 시작`을 누릅니다.

이후 AI 판단과 위험관리 기준을 통과한 주문은 사람의 추가 승인 없이 PC에서 자동 실행됩니다.

## 8. PC 재부팅 시 주의

PC를 재부팅하면 `run_pc_worker.bat`도 다시 실행해야 합니다.

완전 자동으로 켜고 싶다면 Windows 작업 스케줄러에서 로그인 시 `run_pc_worker.bat`을 실행하도록 등록하세요.

## 9. 문제가 생기면

### Dashboard에 계좌가 안 뜸

- PC에서 `run_pc_worker.bat`이 켜져 있는지 확인합니다.
- `.env`의 `DATABASE_URL`이 Render DB의 External Database URL인지 확인합니다.
- PC의 공인 IP가 토스 Open API에 허용되어 있는지 확인합니다.

### 토스 인증 실패 403: IP address not allowed

Worker가 Render에서 돌고 있거나, PC의 현재 공인 IP가 토스에 등록된 IP와 다릅니다.

- Render Worker를 중지/삭제합니다.
- PC에서 `run_pc_worker.bat`만 실행합니다.
- 토스 Open API 허용 IP에 현재 PC 공인 IP를 다시 등록합니다.

### Dashboard 버튼은 눌리는데 주문이 안 됨

- PC `.env`에서 `LIVE_TRADING_ENABLED=true`인지 확인합니다.
- Dashboard에서 `자동매매 시작` 상태인지 확인합니다.
- 운영 기록의 거절 사유를 확인합니다.
