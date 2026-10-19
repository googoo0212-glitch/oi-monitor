# OI 실시간 모니터

- `index.html` — 실시간 가격(하이퍼리퀴드, 5초마다)·계정별 위험·펀딩 추이 페이지 (GitHub Pages)
- `scripts/record_funding.py` + `.github/workflows/funding.yml` — 30분마다 Variational 공개 API에서 금·은 펀딩을 받아 `data/`에 기록
- `data/positions.json` — 계정별 포지션·청산가·SL/TP. **증거금을 옮기거나 포지션을 바꾸면 이 파일만 수정** (GitHub에서 연필 아이콘 → 수정 → Commit)

⚠ 공개 저장소이면 이 페이지와 positions.json은 링크를 아는 누구나 볼 수 있어요. 지갑 주소는 넣지 않았어요.
