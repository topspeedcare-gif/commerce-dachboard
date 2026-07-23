# TOPSPEED V2 — 정산·데이터 트랙 / AI 비서봇 트랙 분리 구조

## 폴더 구조

```
topspeed_v2/
  shared/
    db.py                  # 공유 데이터 레이어 (SQLite) — 두 트랙이 여기로만 연결됨
  dashboard/
    coupang_client.py       # 기존 검증된 오픈API 클라이언트 (그대로 재사용)
    coupang_ads_client.py    # 광고 API 클라이언트 (엔드포인트는 키 발급 후 확인 필요)
    calc.py                  # 실손익·ROAS·CTR·CVR 계산 (순수 함수, 테스트 쉬움)
    sync_job.py              # 하루 한 번 실행하는 배치 (수집→계산→저장→알람)
  assistant/
    dispatcher.py            # 명령어 등록 시스템 (60개 if/elif를 대체)
    commands_coupang.py      # 예시 명령어 2개 (오늘 매출, 알람 확인)
    bot.py                   # Slack 진입점
```

## 왜 이렇게 나눴는가

- `dashboard/`와 `assistant/`는 서로의 파일을 **절대 import하지 않습니다.**
  둘 사이의 유일한 연결고리는 `shared/db.py` 하나뿐입니다.
- 대시보드 로직을 아무리 고쳐도 슬랙 봇 코드는 안 건드려도 됩니다.
- 슬랙 명령어를 추가할 땐 `commands_xxx.py` 새 파일 하나만 만들면 됩니다.
  기존 파일을 열어볼 필요가 없어서, 예전에 있었던 "쿠팡 전체 동기화" 중복 등록
  같은 버그가 애초에 불가능합니다 — 중복 등록하면 즉시 에러가 납니다.

## 설치

```bash
cd topspeed_v2
pip install slack_bolt python-dotenv --break-system-packages
```

## .env 설정 (직접 만드세요)

```
# 쿠팡 오픈API (주문·재고 — 이미 갖고 계신 키)
COUPANG_ACCESS_KEY=
COUPANG_SECRET_KEY=
COUPANG_VENDOR_ID=

# 쿠팡 광고 API (advertising.coupang.com 파트너센터에서 별도 신청 필요)
COUPANG_ADS_ACCESS_KEY=
COUPANG_ADS_SECRET_KEY=
COUPANG_ADS_ACCOUNT_ID=

# 슬랙
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
```

## 실행 순서

1. **원가 파일 준비** — SKU별 원가를 `unit_costs.json`에 `{"SKU001": 15000, ...}` 형태로 작성
2. **일일 동기화 실행** (cron으로 매일 새벽에 자동 실행 추천)
   ```bash
   python dashboard/sync_job.py --unit-cost-file unit_costs.json
   ```
3. **슬랙 봇 실행** — 동기화 결과를 대화로 조회
   ```bash
   python assistant/bot.py
   ```

## 지금 바로 확인해볼 수 있는 것

API 키 없이도 dispatcher와 계산 로직만 이렇게 테스트 가능합니다:

```bash
python3 -c "
from dashboard.calc import calc_daily_metric
print(calc_daily_metric('2026-07-23','SKU001', sales_qty=10, revenue=300000,
    ad_impressions=2000, ad_clicks=20, ad_spend=150000, unit_cost=15000))
"
```

## 다음 단계 (우선순위 순)

1. 광고 API 키 발급받아 `coupang_ads_client.py`의 TODO 엔드포인트 확인·교체
2. `commands_coupang.py`를 참고해 이메일/캘린더/노션 명령어를 같은 패턴으로 이관
   (기존 `slack_app.py`의 process_event 안에 있는 로직을 하나씩 옮기기 —
   한 번에 다 옮기지 말고 명령어 그룹 단위로 이관 후 테스트)
3. 웹 대시보드는 Streamlit이나 Next.js로 `shared/db.py`를 읽기만 하는
   별도 앱으로 구성 (제공된 대시보드 mockup 참고)
