# 🤖 AIRVIS (Jarvis × Multi-AI Agent Assistant)

> **더블 박수(👏 👏)로 깨어나 음성으로 소통하고 작업을 수행하는 멀티 AI 데스크톱 비서**

`hectorg2211/jarvis`의 박수 감지 및 ElevenLabs TTS 시스템을 기반으로, **OpenClaw, Hermes Agent(에르메스), Grok Bot(그록봇)** 등 차세대 AI 에이전트를 자유롭게 넘나들며 음성으로 제어할 수 있는 올인원 AI 비서 시스템입니다.

---

## 📌 주요 특징

### AIRVIS V6 오케스트레이션 엔진

AIRVIS는 더 이상 레지스트리 묶음이 아니라 **실제로 연결된 오케스트레이션 파이프라인**입니다.
하나의 요청은 아래 경로를 그대로 통과합니다.

```text
사용자 요청
   → Orchestrator
   → Planner (Task Decomposer)
   → DAG Engine (독립 작업 동시 실행)
   → AgentRouter (capability / cost / health / policy 스코어링)
   → Agent  →  Backend  →  Provider  →  Model
   → Tool 실행 (권한·위험도 게이트)
   → Artifact / Context 갱신
   → Review (품질 게이트)
        ├─ PASS → 최종 결과
        └─ FAIL → Error Analyzer → Repair Planner
                  → RETRY / REPLAN / CHANGE_AGENT / CHANGE_PROVIDER /
                    CHANGE_MODEL / CHANGE_BACKEND / MODIFY_CONTEXT /
                    REQUEST_APPROVAL / HUMAN_REVIEW / ABORT
                  → 재실행
```

#### 계층 분리 원칙

| 계층 | 책임 | 질문 |
| --- | --- | --- |
| **AIRVIS** | 계획·라우팅·검수·복구 | "무엇을 해야 하는가?" |
| **Backend** | 실행 환경·툴 접근·세션·취소 | "이 에이전트를 어떻게 실행하는가?" |
| **Provider** | 모델 호출 | "어떤 모델이 결과를 생성하는가?" |

에이전트는 `backend_id` / `provider_id` / `model`을 **명시적으로** 선언하며, 등록 시점에
참조 무결성이 검증됩니다. ID 문자열을 쪼개서 백엔드를 추측하는 규칙은 없습니다.

```python
from airvis import AirvisEngine

engine = AirvisEngine()
result = engine.run_sync("이 저장소를 분석해서 버그를 찾고 보고서를 작성해줘")
print(result.status.value, result.output)
```

#### 구성 요소

* **Providers** — OpenAI / Anthropic / Gemini / xAI / OpenRouter / Ollama / custom / mock.
  `ProviderCapabilities`(chat, streaming, tool_calling, vision, structured_output, reasoning,
  embeddings)로 기능을 감지하므로 모든 Provider가 모든 기능을 구현할 필요는 없습니다.
  실패 시 fallback 체인을 따라 자동 전환하고 health/latency/실패율을 기록합니다.
* **Backends** — `native`(인프로세스 에이전트 런타임), `openclaw`, `hermes`(외부 CLI 런타임),
  `mcp`, custom. OpenClaw/Hermes는 Provider가 아니라 **실행 백엔드**이며, 바이너리가 없으면
  성공을 위장하지 않고 `BackendUnavailableError`를 던집니다.
* **Agents** — researcher / debugger / architect / coder / tester / reviewer / committer /
  reporter / generalist. 파이프라인에 하드코딩되어 있지 않고 레지스트리에서 동적으로 선택됩니다.
* **Tools** — 단일 정본 추상화. `filesystem.*`, `terminal.execute`, `git.*`, `web.fetch`,
  `code.analyze`, `test.run` 등 18종. 모든 호출은 권한 → 위험도 → 승인 정책을 거칩니다.
* **Review** — correctness / completeness / security / tests / requirements / regressions /
  code_quality를 실제 증거(도구 결과·테스트 출력·아티팩트)로 채점하고 **반려할 수 있습니다.**
* **Repair** — 실패를 10개 범주로 분류한 뒤 정책 기반으로 전략을 선택하며, 같은 전략을
  두 번 시도하지 않고 모든 경로가 종료됩니다(무한 재시도 없음).
* **Artifacts / Context** — 산출물은 1급 객체이며, 작업은 거대한 원문 대신 아티팩트 참조를
  주고받습니다. 컨텍스트는 예산에 맞춰 압축됩니다.
* **Observability** — `workflow.*`, `task.*`, `agent.selected`, `backend.selected`,
  `provider.selected`, `tool.*`, `review.*`, `repair.*` 구조화 이벤트를 발행하고 SQLite에
  영속화하여 중단된 워크플로를 재개할 수 있습니다.

#### CLI

```bash
airvis status                 # 엔진 구성 요약
airvis health                 # Provider/Backend 실시간 헬스 체크
airvis doctor                 # 의존성·설정·참조 무결성 진단
airvis providers list         # Provider와 capability, health
airvis backends list          # 실행 백엔드
airvis agents list            # 에이전트 선언(backend/provider/model 포함)
airvis agents route "<작업>"   # 라우팅 점수 근거 확인
airvis tools list             # 툴과 위험도
airvis plan "<요청>"           # 실행하지 않고 계획만 확인
airvis workflow run "<요청>"   # 파이프라인 실행
airvis workflow status <id>   # 진행 상황
airvis workflow cancel <id>   # 취소
airvis workflow resume <id>   # 중단된 워크플로 재개
airvis task inspect <id>      # 작업 상세
airvis config                 # 최종 반영된 설정
```

#### 설정

`airvis.example.yaml`을 `airvis.yaml`(프로젝트 루트) 또는 `~/.airvis/airvis.yaml`로 복사하거나
`AIRVIS_CONFIG`로 경로를 지정합니다. JSON도 지원하며 환경 변수가 파일 값을 덮어씁니다.

```yaml
routing:
  strategy: balanced        # cheap | balanced | fast | quality | local_only | premium
agents:
  default_timeout: 300
providers:
  health_check_interval: 30
security:
  default_high_risk_policy: approval
repair:
  max_retries: 3
workflow:
  max_concurrency: 8
```

#### 보안

위험도는 `SAFE → LOW → MEDIUM → HIGH → CRITICAL`이며 기본값은
`filesystem.read=SAFE`, `filesystem.write=MEDIUM`, `filesystem.delete=HIGH`,
`terminal.execute=HIGH`(파괴적 명령은 CRITICAL로 승격되어 차단), `git.commit=HIGH`,
`git.push=CRITICAL` 입니다. 모든 값은 설정으로 조정할 수 있고, 승인 게이트를 우회하는
경로는 없습니다. 툴은 workspace 밖 경로에 접근할 수 없습니다.

#### MCP

`mcp.enabled: true`로 설정하면 MCP 서버의 툴을 stdio JSON-RPC로 발견하여
`mcp.<server>.<tool>` 이름으로 등록합니다. MCP 툴도 동일한 권한·위험도 체계를 따릅니다.

#### 하위 호환

기존 공개 API는 어댑터로 유지됩니다. `airvis.runtime.AgentRuntime`은 그대로 동작하지만
내부적으로 V6 파이프라인을 통해 실행되며, `airvis.provider_manager`, `airvis.model_router`,
`airvis.planning`, `airvis.multiagent`, `airvis.permissions`는 deprecated 어댑터입니다.
신규 코드는 `airvis.AirvisEngine`을 사용하세요.

```bash
AI_ENGINE=native python3 jarvis.py
python3 -m airvis.cli chat "hello"
```

Web API에는 `/health`, `/api/providers`, `/api/backends`, `/api/tools`, `/api/memory`,
`/api/chat`, `/api/agent/run`, `/api/tools/execute`, `/api/workflows`, `/api/plan`,
`/api/events`, `/api/config`가 있습니다. WebSocket은 별도 프로세스로 실행합니다.

```bash
python3 websocket_server.py
```

기본 주소는 `ws://127.0.0.1:8766`이며 `assistant.state`, `assistant.message`, `error`
이벤트를 전송합니다. 원격 바인딩 시에는 `AIRVIS_API_TOKEN`을 설정하고 요청에
`Authorization: Bearer <token>`을 포함해야 합니다.

#### 테스트

```bash
pip install -e ".[test]"
pytest
```

### 음성 비서 기능

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
airvis/                      # V6 오케스트레이션 엔진 (설치 가능한 패키지)
├── engine.py                # 컴포지션 루트: 모든 레지스트리와 파이프라인을 조립
├── core/                    # 예외 · 이벤트 · 설정 · 헬스/워크로드 · async 브릿지
├── providers/               # Provider 인터페이스, HTTP 구현체, 레지스트리, 팩토리
├── backends/                # Backend 인터페이스, native / OpenClaw / Hermes / MCP, 라우터
├── agents/                  # AgentSpec, 레지스트리(참조 검증), AgentRouter, 기본 로스터
├── tools/                   # 단일 정본 Tool 추상화 + filesystem/terminal/git/web/code/test
├── security/                # PermissionManager (위험도 · 정책 · 승인 · 샌드박스)
├── orchestration/           # Task 모델, Planner, DAG 엔진, Review, Repair, Orchestrator
├── context/                 # 컨텍스트 조립 및 압축
├── artifacts/               # 1급 아티팩트와 버전 관리
├── state/                   # SQLite 영속화(워크플로/작업/이벤트/아티팩트) + 장기 기억
├── mcp/                     # MCP stdio 클라이언트와 툴 등록
├── cli.py                   # airvis 명령줄 인터페이스
├── doctor.py                # 설치·설정·참조 무결성 진단
├── compat.py                # V4 공개 API 어댑터
└── runtime.py               # AgentRuntime 파사드 (하위 호환)

jarvis.py                    # 더블 박수 감지, Wake-up, 명령 라우팅 및 음성 비서 메인 루프
engine_bridge.py             # OpenClaw, Hermes, Grokbot 멀티 AI 엔진 통합 디스패처
openclaw_bridge.py           # OpenClaw CLI/Gateway 통신 및 응답 JSON 파싱 브릿지
speech.py                    # 실시간 VAD 음성 인식(STT) 및 ElevenLabs TTS (캐싱 & 폴백)
web_server.py                # 로컬 컨트롤 룸 HTTP API
websocket_server.py          # 실시간 상태 브로드캐스트
config.py                    # .env 및 환경변수 로더 유틸리티
airvis.example.yaml          # 설정 예시
tests/                       # 단위 · 통합 · 실패 경로 · 종단 인수 테스트
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