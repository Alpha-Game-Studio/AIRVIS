# 🤖 AIRVIS 8.2 — Native AI Agent Operating System

> **OpenClaw 같은 사용 경험을 목표로 하지만, 실행 엔진 자체는 AIRVIS가 소유하는 네이티브 에이전트 OS.**

AIRVIS는 특정 외부 에이전트 런타임에 의존하지 않습니다. 계획, 에이전트 루프, 툴 실행, 세션, 컨텍스트, 검수와 복구를 AIRVIS 네이티브 엔진이 담당하고, Provider는 모델만 제공합니다.

## 🧠 핵심 구조

```text
사용자
  ↓
airvis setup / CLI / Channel
  ↓
AIRVIS Orchestrator
  ├─ Planner / Task DAG
  ├─ Agent Router
  ├─ Native Agent Runtime
  ├─ Tool Runtime + Permission Gate
  ├─ Context / Memory / Sessions
  ├─ Review → Repair → Retry
  └─ Artifact / Event / State
          ↓
      Provider Layer
   Ollama / OpenAI / Anthropic / Gemini / xAI / OpenRouter / Mock
          ↓
        Model
```

### 외부 런타임에 대한 원칙

`openclaw`와 `hermes`는 AIRVIS의 핵심 실행 엔진이 아닙니다. AIRVIS는 이들을 사용하지 않아도 완전히 동작해야 하며 기본 런타임은 항상 `native`입니다. 외부 CLI 통합은 향후 선택적 어댑터로 취급할 수 있지만, AIRVIS의 에이전트 루프를 외부 프로그램에 위임하지 않습니다.

## ⚙️ `airvis setup`

처음 실행하면 OpenClaw 스타일의 중앙 설정 경험으로 다음을 한 번에 구성합니다.

```bash
airvis setup
```

설정 대상:

- **Providers** — 기본 Provider와 fallback 체인
- **Channels** — CLI / Telegram / Discord / Slack / Web / iMessage
- **Orchestrator** — routing strategy, concurrency, review, auto-repair
- **Plugins** — 네이티브 확장 목록
- **Skills** — 재사용 가능한 능력/지침 팩
- **Runtime** — 항상 AIRVIS Native Engine

API 키는 설정 파일에 저장하지 않고 환경 변수로 관리합니다.

```bash
airvis status
airvis health
airvis doctor
airvis chat "내 프로젝트의 구조를 분석하고 개선점을 알려줘"
```

기계가 읽는 JSON이 필요하면 `--json`을 사용합니다. 기본 CLI 출력은 사람이 읽는 자연어/요약 형태입니다.

## 🏗️ 구성 요소

* **Native Agent Runtime** — 에이전트 루프, 세션, 컨텍스트와 작업 실행을 AIRVIS 내부에서 처리합니다.
* **Providers** — OpenAI / Anthropic / Gemini / xAI / OpenRouter / Ollama / custom / mock. 모델 호출과 capability를 담당합니다.
* **Orchestrator** — 요청을 계획하고 DAG로 분해하며 에이전트를 라우팅하고 결과를 검수합니다.
* **Agents** — researcher / debugger / architect / coder / tester / reviewer / committer / reporter / generalist.
* **Tools** — filesystem, terminal, git, web, code analysis, test 등. 권한·위험도 정책을 거친 뒤 실행됩니다.
* **Review / Repair** — 결과가 요구사항을 충족하지 못하면 재계획·재실행·수정 전략을 선택합니다.
* **Memory / State** — SQLite 기반 상태와 세션을 유지하여 장기 실행 작업을 지원합니다.
* **Plugins / Skills** — 네이티브 기능을 확장하는 모듈 계층입니다.
* **Channels** — 같은 AIRVIS 에이전트를 CLI나 메시징/웹 채널에서 사용할 수 있도록 하는 입출력 계층입니다.

## 🧪 개발

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
pytest -q
```

## 📜 라이선스

프로젝트의 `LICENSE`를 참조하세요.
