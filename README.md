# 🤖 AIRVIS (Jarvis × Multi-AI Agent Assistant)

> **더블 박수(👏 👏)로 깨어나 음성으로 소통하고 작업을 수행하는 멀티 AI 데스크톱 비서**

`hectorg2211/jarvis`의 박수 감지 및 ElevenLabs TTS 시스템을 기반으로, **OpenClaw, Hermes Agent(에르메스), Grok Bot(그록봇)** 등 차세대 AI 에이전트를 자유롭게 넘나들며 음성으로 제어할 수 있는 올인원 AI 비서 시스템입니다.

---

## 📌 주요 특징

### AIRVIS Native Runtime

기존 OpenClaw/Hermes/Grok 호환 경로를 유지하면서 `native` 엔진을 선택하면 AIRVIS 자체 Runtime이 세션, Mock/OpenAI-compatible Provider, workspace 제한 Tool, 메모리와 권한 상태를 관리합니다. 외부 API 키 없이도 기본 동작을 검증할 수 있습니다.

```bash
AI_ENGINE=native python3 jarvis.py
python3 -m airvis.cli status
python3 -m airvis.cli tools
python3 -m airvis.cli chat "hello"
python3 -m airvis.cli agents
python3 -m airvis.cli plugins
python3 -m airvis.cli doctor
```

Web API에는 `/health`, `/api/providers`, `/api/tools`, `/api/memory`, `/api/chat`, `/api/agent/run`, `/api/tools/execute`가 추가되었습니다. `AIRVIS_PROVIDER=ollama`와 `OLLAMA_MODEL`을 설정하면 OpenAI-compatible Ollama endpoint를 사용할 수 있습니다.

Native Runtime은 `/api/agents`, `/api/agents/delegate`, `/api/plugins`, `/api/tasks`, `/api/scheduler`를 통해 Agent 위임, Plugin 검색, Task와 1회 예약 작업을 제공합니다.

WebSocket은 별도 프로세스로 실행합니다.

```bash
python3 websocket_server.py
```

기본 주소는 `ws://127.0.0.1:8766`이며 `assistant.state`, `assistant.message`, `error` 이벤트를 전송합니다. Provider Manager는 등록 순서대로 실패한 Provider를 건너뛰고 다음 Provider를 시도합니다.

원격 바인딩 시에는 `AIRVIS_API_TOKEN`을 설정하고 요청에 `Authorization: Bearer <token>`을 포함해야 합니다. 토큰 없이 원격 바인딩하면 모든 요청이 거부됩니다.

Model Catalog는 `/api/models`에서 확인할 수 있고, 환경 점검은 `python3 -m airvis.cli doctor` 또는 `/api/doctor`로 실행합니다.

Provider 장애 대비 fallback은 `AIRVIS_FALLBACK_PROVIDER=ollama`처럼 설정합니다. Native Runtime은 주 Provider 실패 시 fallback Provider를 순서대로 시도합니다.

* **👏 더블 박수(Double-Clap) 웨이크업**: 마이크로 두 번의 박수를 감지하면 즉시 자비스가 깨어납니다.
* **⚡ 초고속 실시간 음성 대화 (VAD)**: 말이 끝나면 0.5초 만에 자동으로 감지하여 지연 없이 빠르게 응답합니다.
* **🚀 멀티 AI 엔진 지원 (Multi-Engine)**:
  * **[OpenClaw](https://docs.openclaw.ai) (오픈클로)**: 로컬 도구 실행 및 시스템 자동화에 최적화된 게이트웨이 에이전트.
  * **[Hermes Agent](https://hermes-agent.nousresearch.com/) (에르메스)**: Nous Research의 자율 학습형 고지능 AI 에이전트 (`hermes` CLI, Nous Portal, OpenRouter 연동).
  * **[Grok Bot](https://x.ai/news/introducing-grok-bot) (그록봇)**: xAI의 24/7 상시 자율 에이전트 시스템 (xAI API, Grok-2, OpenRouter 연동).
* **🔄 실시간 음성 엔진 전환**: 대화 도중 *"엔진 에르메스로 바꿔줘"*, *"그록봇 엔진으로 변경해"* 등의 음성 명령으로 AI 엔진을 즉시 스위칭 가능.
* **🧠 2-Track 명령 라우팅**:
  * **로컬 데스크톱 제어**: 크롬, 스포티파이, 유튜브, Cursor 에디터 등을 즉각 실행.
  * **AI 에이전트 위임**: 최신 뉴스, 날씨, 파일 탐색, 복합 작업 등은 선택된 AI 엔진이 처리.
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
 (OpenClaw/Hermes) (Chrome, Spotify 등) ┌───────────────────────────┐
                                        │ 1. OpenClaw Gateway       │
                                        │ 2. Hermes Agent (Nous)    │
                                        │ 3. Grok Bot (xAI)         │
                                        └───────────────────────────┘
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

### 로컬 컨트롤 룸 UI

별도 의존성 없이 브라우저에서 명령 실행, 상태 확인, 음성 및 라우팅 세팅을 관리할 수 있습니다.

```bash
python3 web_server.py
```

브라우저에서 `http://127.0.0.1:8765`를 열어 사용하세요. UI는 localhost에만 바인딩되며, 음성 기능은 기존 `python3 jarvis.py` 프로세스에서 계속 사용할 수 있습니다.

### 1. 필수 사전 준비

#### 1) AI 엔진 설정 (택 1 이상)
* **OpenClaw (기본 권장)**: `openclaw status`로 게이트웨이 확인
* **Hermes Agent**: [Hermes Agent 공식 사이트](https://hermes-agent.nousresearch.com/) 또는 OpenRouter API 키
* **Grok Bot**: [xAI Grok Bot 공식 페이지](https://x.ai/news/introducing-grok-bot) 또는 xAI API 키

#### 2) Python 3.10+ 환경 및 패키지 설치
```bash
python3 -m pip install -e .
```

이후 `airvis` 명령을 직접 사용할 수 있습니다. 음성 기능까지 사용하려면 기존 `requirements.txt`도 설치하세요.

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
# 3. Hermes Agent (Nous Research) 설정
# ==========================================
# HERMES_API_KEY=sk-or-v1-... (OpenRouter 키 또는 Nous Portal 키)
# HERMES_BASE_URL=https://openrouter.ai/api/v1
# HERMES_MODEL=nousresearch/hermes-3-llama-3.1-405b

# ==========================================
# 4. Grok Bot (xAI) 설정
# ==========================================
# XAI_API_KEY=xai-... (xAI Console API 키)
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
* **"엔진 에르메스로 바꿔줘"** / **"에르메스 켜줘"** ➔ *"AI 엔진을 에르메스(Hermes Agent)로 변경했습니다."*
* **"그록봇 엔진으로 변경해"** / **"그록으로 바꿔"** ➔ *"AI 엔진을 그록봇(Grok Bot)로 변경했습니다."*
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
| `HERMES_API_KEY` | - | [Hermes Agent](https://hermes-agent.nousresearch.com/)용 API 키 (OpenRouter / Nous Portal) |
| `XAI_API_KEY` | - | [Grok Bot](https://x.ai/news/introducing-grok-bot)용 xAI API 키 |
| `JARVIS_CONVERSATION_MODE` | `false` | 멀티턴 연속 대화 모드 활성화 여부 |
| `JARVIS_VAD_SILENCE_SECONDS` | `0.5` | 말 끝난 후 무음 판정 시간(초) - 작을수록 빠른 응답 |
| `JARVIS_VAD_THRESHOLD` | `0.025` | 음성 시작 감지 볼륨 임계값 |
| `JARVIS_STT_PROVIDER` | `speech_recognition` | STT 엔진 (`speech_recognition` / `openai`) |
| `JARVIS_STT_LANGUAGE` | `ko-KR` | 음성 인식 언어 코드 |
| `JARVIS_WAKE_PROMPT` | `Yes, sir. 무엇을 도와드릴까요?` | 박수 감지 시 자비스의 첫 인사말 |
| `JARVIS_TTS_CACHE_ENABLED` | `true` | 자주 쓰이는 음성 로컬 캐싱으로 속도 향상 |
| `JARVIS_MACOS_VOICE` | `Yuna` | ElevenLabs 오류 시 대체할 Mac 내장 음성 |

---

## 🔗 공식 레퍼런스

* **OpenClaw Gateway**: [https://docs.openclaw.ai](https://docs.openclaw.ai)
* **Hermes Agent (Nous Research)**: [https://hermes-agent.nousresearch.com/](https://hermes-agent.nousresearch.com/)
* **Grok Bot (xAI)**: [https://x.ai/news/introducing-grok-bot](https://x.ai/news/introducing-grok-bot)