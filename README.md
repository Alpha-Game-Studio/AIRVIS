# 🤖 AIRVIS (Jarvis × Multi-AI Agent Assistant)

> **더블 박수(👏 👏)로 깨어나 음성으로 소통하고 작업을 수행하는 멀티 AI 데스크톱 비서**

`hectorg2211/jarvis`의 박수 감지 및 ElevenLabs TTS 시스템을 기반으로, **OpenClaw, Hermes(에르메스), Grokbot(그록봇)** 등 다양한 AI 엔진을 자유롭게 넘나들며 음성으로 제어할 수 있는 올인원 AI 비서 시스템입니다.

---

## 📌 주요 특징

* **👏 더블 박수(Double-Clap) 웨이크업**: 마이크로 두 번의 박수를 감지하면 즉시 자비스가 깨어납니다.
* **⚡ 초고속 실시간 음성 대화 (VAD)**: 말이 끝나면 0.5초 만에 자동으로 감지하여 지연 없이 빠르게 응답합니다.
* **🚀 멀티 AI 엔진 지원 (Multi-Engine)**:
  * **OpenClaw (오픈클로)**: 로컬 도구 실행 및 시스템 자동화에 최적화된 게이트웨이 에이전트.
  * **Hermes (에르메스)**: Nous Research의 고지능 오픈소스 AI 에이전트 (OpenRouter / API / CLI 지원).
  * **Grokbot (그록봇)**: xAI의 Grok 모델 기반 에이전트 (xAI API / OpenRouter / CLI 지원).
* **🔄 실시간 음성 엔진 전환**: 대화 도중 *"엔진 에르메스로 바꿔줘"*, *"그록봇 엔진으로 변경해"* 등의 음성 명령으로 AI 엔진을 즉시 스위칭 가능.
* **🧠 2-Track 명령 라우팅**:
  * **로컬 데스크톱 제어**: 크롬, 스포티파이, 유튜브, Cursor 에디터 등을 즉각 실행.
  * **AI 에이전트 위임**: 최신 뉴스, 날씨, 파일 탐색, 복합 질의 등은 선택된 AI 엔진이 처리.
* **🗣️ ElevenLabs AI 음성 & 2중 안전 폴백**: 고품질 AI 보이스로 말하며, API 오류 시에도 시스템 기본 음성(macOS Yuna)으로 끊김 없이 재생.
* **💬 멀티턴 연속 대화 모드**: 한 번의 박수로 "종료"라고 말할 때까지 자연스럽게 질문을 이어갈 수 있습니다.

---

## 🏗️ 전체 동작 흐름

```text
       👏 👏 (더블 박수 감지)
              ↓
    Jarvis Wake-up 활성화
              ↓
  "Yes, sir. 무엇을 도와드릴까요?" (ElevenLabs TTS)
              ↓
    사용자 음성 입력 (실시간 VAD)
              ↓
    음성 인식 (STT: Google / Whisper)
              ↓
        텍스트 명령 생성
              ↓
     [ 명령 라우팅 계층 (handle_command) ]
      /                  |                 \
 (엔진 전환 명령)   (로컬 제어 명령)     (AI 질의 / 복합 작업)
      ↓                  ↓                  ↓
  AI 엔진 변경     Jarvis Desktop 제어   [ 활성 AI 엔진 ]
 (OpenClaw/Hermes) (Chrome, Spotify 등) ┌───────────────┐
                                        │ 1. OpenClaw   │
                                        │ 2. Hermes     │
                                        │ 3. Grokbot    │
                                        └───────────────┘
      \                  |                 /
       \                 |                /
              ↓
          결과 텍스트 반환
              ↓
       ElevenLabs TTS 음성 출력
              ↓
    (대화 모드 활성화 시) 연속 청취 루프
```

---

## 📁 파일 구조

```text
├── jarvis.py              # 더블 박수 감지, Wake-up, 명령 라우팅 및 음성 비서 메인 루프
├── engine_bridge.py       # OpenClaw, Hermes, Grokbot 멀티 AI 엔진 통합 디스패처
├── openclaw_bridge.py     # OpenClaw CLI/Gateway 통신 및 응답 JSON 파싱 브릿지
├── speech.py              # 실시간 VAD 음성 인식(STT) 및 ElevenLabs TTS (캐싱 & 폴백)
├── config.py              # .env 및 환경변수 로더 유틸리티
├── requirements.txt       # 의존성 패키지 목록
├── .env                   # API 키 및 환경 설정 (Git 제외)
└── README.md              # 프로젝트 설명서
```

---

## 🚀 빠른 시작 (Setup & Run)

### 1. 필수 사전 준비

#### 1) OpenClaw 설치 및 실행 (기본 권장)
```bash
openclaw status
openclaw agents list
```

#### 2) Python 3.10+ 환경 및 패키지 설치
```bash
python3 -m pip install -r requirements.txt
```

---

### 2. 환경 변수 설정 (`.env`)

프로젝트 루트 폴더에 `.env` 파일을 만들고 아래 내용을 입력합니다:

```env
# ==========================================
# 1. ElevenLabs TTS 설정 (필수)
# ==========================================
ELEVENLABS_API_KEY=your_elevenlabs_api_key
ELEVENLABS_VOICE_ID=pNInz6obpgDQGcFmaJgB
# ※ Free 티어 계정은 pNInz6obpgDQGcFmaJgB(Adam) 또는 JBFqnCBsd6RMkjVDRZzb(George) 권장

# ==========================================
# 2. 기본 AI 엔진 선택 (openclaw | hermes | grokbot)
# ==========================================
AI_ENGINE=openclaw

# ==========================================
# 3. Hermes (에르메스) 설정 (선택 사항)
# ==========================================
# OpenRouter 키 또는 직접 API 키 사용 시 설정:
# HERMES_API_KEY=sk-or-v1-...
# HERMES_BASE_URL=https://openrouter.ai/api/v1
# HERMES_MODEL=nousresearch/hermes-3-llama-3.1-405b

# ==========================================
# 4. Grokbot (그록봇) 설정 (선택 사항)
# ==========================================
# xAI API 키 또는 OpenRouter 키 사용 시 설정:
# XAI_API_KEY=xai-...
# GROK_MODEL=grok-2-latest

# ==========================================
# 5. OpenClaw 에이전트 설정
# ==========================================
OPENCLAW_CLI=openclaw
OPENCLAW_AGENT=main
OPENCLAW_SESSION_KEY=jarvis
OPENCLAW_TIMEOUT=120

# ==========================================
# 6. 대화 모드 & 실시간 음성 감도 (VAD) 튜닝
# ==========================================
JARVIS_CONVERSATION_MODE=true
JARVIS_VAD_SILENCE_SECONDS=0.5
JARVIS_VAD_THRESHOLD=0.025

# ==========================================
# 7. 음성 인식 (STT) 설정
# ==========================================
JARVIS_STT_PROVIDER=speech_recognition
JARVIS_STT_LANGUAGE=ko-KR

# ==========================================
# 8. 자비스 멘트 & 로컬 바로가기
# ==========================================
JARVIS_WAKE_PROMPT=Yes, sir. 무엇을 도와드릴까요?
JARVIS_CHROME_URL=https://www.google.com
JARVIS_YOUTUBE_URL=https://www.youtube.com
SONG_URI=https://open.spotify.com/track/39shmbIHICJ2Wxnk1fPSdz?si=2900c75c2e2d4b82
```

---

### 3. Jarvis 실행

```bash
python3 jarvis.py
```

프로그램이 시작되면 **박수를 2회(👏 👏)** 치세요!

---

## 💡 음성 명령 예시

### 1. AI 엔진 실시간 전환 (Multi-Engine Switch)
* **"엔진 에르메스로 바꿔줘"** / **"에르메스 켜줘"** ➔ *"AI 엔진을 에르메스(Hermes)로 변경했습니다."*
* **"그록봇 엔진으로 변경해"** / **"그록으로 바꿔"** ➔ *"AI 엔진을 그록봇(Grokbot)로 변경했습니다."*
* **"오픈클로 엔진으로 바꿔줘"** ➔ *"AI 엔진을 오픈클로(OpenClaw)로 변경했습니다."*
* **"현재 엔진 뭐야?"** ➔ *"현재 AI 엔진은 [오픈클로/에르메스/그록봇]입니다."*

### 2. 데스크톱 로컬 제어 (Jarvis 직접 실행)
* **"크롬 열어줘"** ➔ Google Chrome 실행
* **"스포티파이 켜줘"** / **"노래 틀어줘"** ➔ Spotify 실행 및 음악 재생
* **"유튜브 열어줘"** ➔ YouTube 새 창 열기
* **"커서 열어줘"** ➔ Cursor IDE 에디터 실행
* **"클로드 열어줘"** ➔ Claude AI 웹페이지 열기

### 3. AI 에이전트 작업 (선택된 엔진이 처리)
* **"오늘 서울 날씨 어때?"** ➔ 실시간 날씨 정보 검색 후 음성 안내
* **"최신 주요 뉴스 요약해줘"** ➔ 뉴스 요약 브리핑
* **"다운로드 폴더에서 오늘 받은 파일 찾아줘"** ➔ 파일 탐색 및 작업 수행
* **"파이썬으로 웹스크래핑하는 코드 간단히 설명해줘"** ➔ 코딩 질의응답

### 4. 대화 종료
* **"종료"**, **"끝"**, **"그만"**, **"stop"** ➔ *"네, 필요하시면 언제든 박수를 두 번 쳐주세요."* 안내 후 박수 대기 모드로 복귀

---

## ⚙️ 설정 옵션 레퍼런스 (`.env`)

| 환경 변수 | 기본값 | 설명 |
| :--- | :---: | :--- |
| `AI_ENGINE` | `openclaw` | 기본 AI 엔진 (`openclaw` / `hermes` / `grokbot`) |
| `ELEVENLABS_API_KEY` | - | ElevenLabs API 키 |
| `ELEVENLABS_VOICE_ID` | `pNInz6obpgDQGcFmaJgB` | 사용할 ElevenLabs 음성 ID (Adam) |
| `HERMES_API_KEY` | - | Hermes용 API 키 (OpenRouter 또는 direct) |
| `XAI_API_KEY` | - | Grokbot용 xAI API 키 |
| `JARVIS_CONVERSATION_MODE` | `false` | 멀티턴 연속 대화 모드 활성화 여부 |
| `JARVIS_VAD_SILENCE_SECONDS` | `0.5` | 말 끝난 후 무음 판정 시간(초) - 작을수록 빠른 응답 |
| `JARVIS_VAD_THRESHOLD` | `0.025` | 음성 시작 감지 볼륨 임계값 |
| `JARVIS_STT_PROVIDER` | `speech_recognition` | STT 엔진 (`speech_recognition` / `openai`) |
| `JARVIS_STT_LANGUAGE` | `ko-KR` | 음성 인식 언어 코드 |
| `JARVIS_WAKE_PROMPT` | `Yes, sir. 무엇을 도와드릴까요?` | 박수 감지 시 자비스의 첫 인사말 |
| `JARVIS_TTS_CACHE_ENABLED` | `true` | 자주 쓰이는 음성 로컬 캐싱으로 속도 향상 |
| `JARVIS_MACOS_VOICE` | `Yuna` | ElevenLabs 오류 시 대체할 Mac 내장 음성 |

---

## 🛠️ 문제 해결 (Troubleshooting)

* **박수가 감지되지 않을 때**: 마이크와 조금 더 가까운 위치에서 또렷하게 👏 👏 치시거나 `jarvis.py` 상단의 `SPIKE_RATIO`를 약간 낮춰보세요.
* **OpenClaw 연결 오류가 날 때**: 터미널에서 `openclaw status`를 실행하여 게이트웨이 서비스가 정상 동작 중인지 확인하세요.
* **엔진을 전환하고 싶을 때**: 음성으로 *"엔진 에르메스로 바꿔줘"* 또는 *"엔진 그록봇으로 변경해"*라고 말하면 즉시 전환됩니다.
* **ElevenLabs 402 에러 발생 시**: Free 티어 계정은 Community Library Voice 사용이 제한되므로, 프리셋 음성인 `pNInz6obpgDQGcFmaJgB`(Adam) 또는 `JBFqnCBsd6RMkjVDRZzb`(George)을 사용하세요.