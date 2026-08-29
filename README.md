# 🤖 AIRVIS 8.2 — Native AI Agent Operating System

> **Research CLI / modern agent CLI 스타일의 사용 경험 + AIRVIS 네이티브 실행 엔진.**

AIRVIS는 계획, 에이전트 루프, 툴 실행, 세션, 컨텍스트, 검수와 복구를 자체 엔진에서 처리합니다. Provider는 모델을 제공하고, CLI는 그 엔진을 직접 조작하는 제품 인터페이스입니다.

## 🚀 설치

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install pytest pytest-asyncio pyyaml
```

음성 기능:

```bash
python -m pip install -e '.[voice]'
```

설치 후 어디서든:

```bash
airvis --help
airvis --version
airvis init
```

## 🖥️ CLI

AIRVIS의 기본 실행 파일은 `airvis`입니다. 별도 더미 CLI가 아니라 현재 AIRVIS 엔진/Provider/Tool/State를 그대로 사용합니다.

```bash
# 첫 설정
airvis init

# 현재 연결/설정 확인
airvis list
airvis platforms
airvis status
airvis health
airvis doctor

# 모델
airvis model list
airvis model config show
airvis model config set openrouter openai/gpt-5-mini
airvis model select openrouter openai/gpt-5-mini

# 에이전트 작업
airvis
# 또는
airvis chat "현재 프로젝트 구조를 분석해줘"
airvis research "버그를 찾아서 수정해줘"

# 도구 discovery / 실행
airvis actions list
airvis actions search "git"
airvis actions knowledge filesystem.read
airvis actions execute filesystem.read '{"path":"README.md"}' --confirm

# Durable workflow
airvis flow run "프로젝트 테스트를 실행하고 실패 원인을 수정해줘"
airvis flow list

airvis flow status <workflow-id>

# 메모리
airvis mem add "프로젝트의 기본 브랜치는 main"
airvis mem search "기본 브랜치"
airvis mem list
```

### Interactive shell

인자 없이 `airvis`를 실행하면 대화형 에이전트 셸이 열립니다.

```text
airvis — native AI agent
Type /help for commands, /voice for voice mode, /exit to quit.

you › 현재 프로젝트 구조를 분석해줘
airvis › ...
```

슬래시 명령도 제공합니다.

- `/help` — CLI 가이드
- `/status` — 엔진 상태
- `/models` — 현재 모델
- `/voice` — 음성 모드
- `/exit` — 종료

### Agent/JSON mode

다른 프로그램이나 AI Agent가 AIRVIS를 호출할 때는 구조화된 출력을 사용할 수 있습니다.

```bash
airvis --agent status
airvis --agent platforms
airvis --agent actions list
airvis --agent chat "inspect this project"
```

## 🎙️ 음성 비서

원할 때만 `voice` 채널을 사용합니다.

```bash
airvis voice
```

흐름은 실제 마이크 → STT → AIRVIS Native Engine → ElevenLabs TTS입니다.

- STT: OpenAI Whisper API
- TTS: ElevenLabs
- macOS: `afplay`로 재생
- 일반 채팅과 동일한 AIRVIS Agent/Tool/Permission/State 경로 사용

API 키는 `~/.airvis/credentials.env`에 저장되며 setup에서 입력합니다. 음성 의존성은 기본 설치에 강제로 포함하지 않습니다.

## 🧠 핵심 구조

```text
사용자
  ↓
airvis CLI / Voice / Channel
  ↓
AIRVIS Native Orchestrator
  ├─ Planner / Task DAG
  ├─ Agent Router
  ├─ Native Agent Runtime
  ├─ Tool Runtime + Permission Gate
  ├─ Context / Memory / Sessions
  ├─ Review → Repair → Retry
  └─ Artifact / Event / State
          ↓
      Provider Layer
   Ollama / OpenAI / Anthropic / Gemini / xAI / OpenRouter
          ↓
        Model
```

## 🔌 Provider / Setup

`airvis init`은 Provider, 모델, fallback, Channel, Voice, orchestration 전략을 한 번에 구성합니다. 이후 필요한 영역만 다시 설정할 수 있습니다.

```bash
airvis setup
airvis login
airvis model config set openrouter openai/gpt-5-mini
```

외부 런타임을 AIRVIS의 실행 엔진으로 사용하지 않습니다. `openclaw` 등은 선택적인 연동 대상이며 기본 실행 권한은 AIRVIS Native Engine에 있습니다.

## 🧩 Tools / Plugins / Skills

AIRVIS의 CLI discovery는 실제 Tool Registry를 조회합니다.

```bash
airvis actions list
airvis plugins
airvis skills
```

새 기능은 더미 응답을 추가하는 방식이 아니라 기존 registry, permission, provider, state 계층에 연결하는 방식으로 구현합니다.

## 🧪 테스트

```bash
python -m pytest -q
```

테스트 의존성이 없는 새 가상환경에서는 먼저:

```bash
python -m pip install -e .
python -m pip install pytest pytest-asyncio pyyaml
```

## 📜 라이선스

프로젝트의 `LICENSE`를 참조하세요.
