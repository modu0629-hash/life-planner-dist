# 📅 개인 생활 플래너

학원 서버에 올리는 개인 생활 계획표. **백엔드 1개(Flask+SQLite) + 클라이언트 여러 개(웹 PWA / 안드 / 아이폰)**가 같은 데이터를 공유한다. (배포 패턴은 `sms-sender`와 동일 — docker `--network host` + Tailscale Funnel)

## 기능 (Phase 1 — 현재)
- **주간 뷰가 기본**, 월/년 뷰 전환. 일정 추가/수정/삭제
- **반복**(매일/매주/매주 요일지정/매월/매년), **⭐ 중요 체크**
- **완료/미완료 체크** — 미완료 시 사유를 프리셋 토글에서 선택, "직접입력" 선택 시 자유 입력 (통계용)
- **주간 완료율** + 통계(장소별 달성률, 미완료 사유 집계)
- **장소별 강제 체크인**(집/학원1/학원2/화천/여주/직접입력) — 없으면 "없음" 체크
- **자동입력** — 카톡/문자/메모 텍스트를 붙여넣으면 Claude가 일정으로 변환
  - 출처 `학원1 자동` → **검토 대기 큐**에 들어가고, 승인해야 일정 확정
- **PWA** — 폰 브라우저에서 "홈 화면에 추가"하면 앱처럼 사용

## 로드맵
- **Phase 2** — 알림: 월/금 12시 입력알람, 빈 요일 아침알람(일정 있는 날 skip). 웹푸시 → FCM
- **Phase 3** — 네이티브 앱: 안드(푸시 + 카톡/문자 **알림 자동읽기** → 학원1 자동제안), 아이폰(푸시 + 입력)
- **Phase 4** — 학원 1년 사이클 반복 알림(annual_cycle 테이블, 차차 채움)

## 로컬 실행
```bash
cp config.example.json config.json   # ui_password, anthropic_api_key 등 채우기(선택)
pip install -r requirements.txt
python app.py                        # http://127.0.0.1:5558
```
- `anthropic_api_key`를 넣어야 "자동입력"(메시지→일정 변환)이 동작한다. 모델 기본값은 `claude-haiku-4-5-20251001`(파싱용, 저렴).
- `ui_password`를 비워두면 로그인 없이 열림. 외부 노출(Funnel) 시 **반드시 설정**.

## 학원 서버 배포 (docker + Tailscale Funnel)
```bash
# 1) 서버로 폴더 복사 후
cd ~/life-planner
cp config.example.json config.json    # ui_password / anthropic_api_key 채우기
docker build -t life-planner .
docker run -d --name life-planner --restart unless-stopped \
  --network host -v ~/life-planner:/app life-planner
curl localhost:5558/api/health        # {"ok":true,...}

# 2) Tailscale Funnel로 외부 노출 (443=sms웹, 8443=sms게이트웨이 점유 중 → 10000 사용)
tailscale funnel --bg --https=10000 localhost:5558
tailscale funnel status               # https://<호스트>.ts.net:10000
```
- 폰에서 `https://<호스트>.ts.net:10000` 접속 → "홈 화면에 추가"로 앱처럼.
- DB(`planner.db`)와 `config.json`은 마운트한 `~/life-planner`에 영구 보관된다.

## 보안
- Funnel은 공개 주소다. **`ui_password`가 유일한 보안벽** — 길게 설정.
- `config.json`, `planner.db`는 `.gitignore` 처리됨(저장소에 올리지 말 것).

## 파일 구성
```
life-planner/
├─ app.py                     # Flask 백엔드 (API + DB + Claude 파싱)
├─ templates/index.html       # 단일 페이지 PWA (모든 탭)
├─ static/
│  ├─ manifest.webmanifest    # PWA 매니페스트
│  ├─ sw.js                   # service worker (캐시 + 푸시 자리)
│  └─ icon.svg                # 앱 아이콘
├─ config.example.json        # 설정 템플릿
├─ Dockerfile
├─ requirements.txt
└─ planner.db                 # SQLite (자동 생성, gitignore)
```

## DB 스키마 요약
- `plans` — 일정 정의(제목/장소/시간/중요/반복규칙/source·review_status)
- `occurrences` — 날짜별 발생 + 완료상태/미완료사유 (반복은 조회 시 lazy 전개)
- `daily_checkin` — 장소별 강제 입력(날짜×장소 1건, 없음 플래그)
- `annual_cycle` — 학원 연간 사이클(Phase 4)
- `devices` / `settings` — 푸시 토큰·설정(Phase 2~)
