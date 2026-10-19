# OI 실시간 모니터

- `index.html` — 실시간 가격(하이퍼리퀴드, 5초마다)·계정별 위험·펀딩 추이 페이지 (GitHub Pages)
- `scripts/record_funding.py` + `.github/workflows/funding.yml` — 30분마다 Variational 공개 API에서 금·은 펀딩을 받아 `data/`에 기록
- `data/positions.json` — 계정별 포지션·청산가·SL/TP. **증거금을 옮기거나 포지션을 바꾸면 이 파일만 수정** (GitHub에서 연필 아이콘 → 수정 → Commit)

⚠ 공개 저장소이면 이 페이지와 positions.json은 링크를 아는 누구나 볼 수 있어요. 지갑 주소는 넣지 않았어요.

## 텔레그램 알림
- `.github/workflows/alerts.yml` — 5분마다 현재가(하이퍼리퀴드)로 SL·TP·청산가까지 거리 확인 → 가까워지면 알림
- 알림 기준은 `data/alert_config.json` (금 1.5%/0.7%, 은 2.5%/1.2%)
- Settings → Secrets and variables → Actions 에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 등록 필요
