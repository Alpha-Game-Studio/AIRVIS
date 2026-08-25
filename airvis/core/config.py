"""Declarative configuration for AIRVIS.

Precedence (lowest to highest): dataclass defaults, config file, environment
variables, explicit overrides passed to :meth:`AirvisConfig.load`.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .errors import ConfigError

CONFIG_FILENAMES = ("airvis.yaml", "airvis.yml", "airvis.json", ".airvis.yaml")


class RoutingStrategy(str, Enum):
    CHEAP = "cheap"
    BALANCED = "balanced"
    FAST = "fast"
    QUALITY = "quality"
    LOCAL_ONLY = "local_only"
    PREMIUM = "premium"


#: Per-strategy scoring weights consumed by :class:`airvis.agents.router.AgentRouter`.
STRATEGY_WEIGHTS: dict[RoutingStrategy, dict[str, float]] = {
    RoutingStrategy.BALANCED: {
        "capability": 4.0, "reliability": 1.0, "health": 1.5, "priority": 1.0,
        "cost": 1.0, "latency": 0.6, "workload": 1.0, "quality": 1.0, "locality": 0.0,
    },
    RoutingStrategy.CHEAP: {
        "capability": 4.0, "reliability": 0.6, "health": 1.0, "priority": 0.4,
        "cost": 4.0, "latency": 0.2, "workload": 0.6, "quality": 0.2, "locality": 1.0,
    },
    RoutingStrategy.FAST: {
        "capability": 4.0, "reliability": 0.8, "health": 1.5, "priority": 0.6,
        "cost": 0.2, "latency": 3.5, "workload": 2.0, "quality": 0.4, "locality": 0.5,
    },
    RoutingStrategy.QUALITY: {
        "capability": 4.0, "reliability": 2.0, "health": 1.5, "priority": 1.5,
        "cost": 0.1, "latency": 0.1, "workload": 0.4, "quality": 3.5, "locality": 0.0,
    },
    RoutingStrategy.PREMIUM: {
        "capability": 4.0, "reliability": 2.0, "health": 1.0, "priority": 2.0,
        "cost": 0.0, "latency": 0.0, "workload": 0.2, "quality": 5.0, "locality": 0.0,
    },
    RoutingStrategy.LOCAL_ONLY: {
        "capability": 4.0, "reliability": 1.0, "health": 1.5, "priority": 1.0,
        "cost": 2.0, "latency": 0.5, "workload": 1.0, "quality": 0.5, "locality": 6.0,
    },
}


@dataclass
class RoutingConfig:
    strategy: str = RoutingStrategy.BALANCED.value
    weights: dict[str, float] = field(default_factory=dict)
    #: minimum score an agent must reach to be selected at all
    min_score: float = -1e9
    #: skip agents whose health tracker reports them as unhealthy
    skip_unhealthy: bool = True

    def resolved_weights(self) -> dict[str, float]:
        try:
            strategy = RoutingStrategy(str(self.strategy).lower())
        except ValueError as exc:
            raise ConfigError(f"unknown routing strategy: {self.strategy}") from exc
        merged = dict(STRATEGY_WEIGHTS[strategy])
        merged.update({key: float(value) for key, value in self.weights.items()})
        return merged


@dataclass
class AgentsConfig:
    default_timeout: float = 300.0
    default_max_concurrency: int = 4
    default_backend: str = "native"


@dataclass
class ProvidersConfig:
    default: str = ""
    fallbacks: list[str] = field(default_factory=list)
    health_check_interval: float = 30.0
    request_timeout: float = 60.0
    max_output_tokens: int = 2048


@dataclass
class BackendsConfig:
    enabled: list[str] = field(default_factory=lambda: ["native"])
    openclaw_command: str = "openclaw"
    hermes_command: str = "hermes"
    execute_timeout: float = 180.0
    max_iterations: int = 6
    max_tool_calls: int = 20


@dataclass
class SecurityConfig:
    #: risk level at or below which execution is approved automatically
    auto_approve_max_risk: str = "LOW"
    #: policy applied to anything above ``auto_approve_max_risk``
    default_high_risk_policy: str = "approval"  # approval | allow | deny
    #: per-tool policy overrides, e.g. {"git.push": "deny"}
    tool_policies: dict[str, str] = field(default_factory=dict)
    #: per-tool risk overrides, e.g. {"terminal.execute": "CRITICAL"}
    risk_overrides: dict[str, str] = field(default_factory=dict)
    denied_tools: list[str] = field(default_factory=list)
    workspace_restricted: bool = True
    allow_network: bool = True
    #: extra directories a tool may touch besides the workspace
    additional_writable_paths: list[str] = field(default_factory=list)


@dataclass
class RepairConfig:
    max_retries: int = 3
    max_repairs_per_task: int = 4
    max_repairs_per_workflow: int = 12
    retry_backoff_seconds: float = 0.5
    #: category -> ordered strategy names, overrides the built-in playbook
    strategies: dict[str, list[str]] = field(default_factory=dict)
    allow_human_review: bool = True


@dataclass
class WorkflowConfig:
    max_concurrency: int = 8
    task_timeout: float = 300.0
    persist: bool = True
    cancel_dependents_on_failure: bool = True
    fail_fast: bool = False


@dataclass
class ReviewConfig:
    enabled: bool = True
    min_score: float = 0.6
    dimensions: list[str] = field(
        default_factory=lambda: [
            "correctness", "completeness", "security", "tests",
            "requirements", "regressions", "code_quality",
        ]
    )
    use_llm_reviewer: bool = False
    reviewer_capability: str = "review"


@dataclass
class ContextConfig:
    max_chars: int = 12000
    max_messages: int = 24
    max_previous_results: int = 6
    include_artifacts: bool = True
    compression: str = "truncate"  # truncate | summarize | none


@dataclass
class MCPServerConfig:
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    default_risk: str = "MEDIUM"


@dataclass
class MCPConfig:
    enabled: bool = False
    servers: list[MCPServerConfig] = field(default_factory=list)
    connect_timeout: float = 20.0


@dataclass
class StateConfig:
    enabled: bool = True
    path: str = ""

    def resolved_path(self) -> Path:
        return Path(self.path).expanduser() if self.path else Path.home() / ".airvis" / "state.db"


@dataclass
class AirvisConfig:
    workspace: str = ""
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    backends: BackendsConfig = field(default_factory=BackendsConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    repair: RepairConfig = field(default_factory=RepairConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    state: StateConfig = field(default_factory=StateConfig)
    source: str = "defaults"

    # -- construction ---------------------------------------------------------

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        environ: dict[str, str] | None = None,
        overrides: dict[str, Any] | None = None,
        search_from: str | Path | None = None,
    ) -> AirvisConfig:
        environ = dict(os.environ if environ is None else environ)
        data: dict[str, Any] = {}
        source = "defaults"
        config_path = _resolve_config_path(path, environ, search_from)
        if config_path is not None:
            data = _read_config_file(config_path)
            source = str(config_path)
        config = cls.from_dict(data)
        config.source = source
        config.apply_environment(environ)
        if overrides:
            config = cls.from_dict(_deep_merge(config.to_dict(), overrides))
            config.source = source
        if not config.workspace:
            config.workspace = str(Path.cwd())
        return config

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AirvisConfig:
        if not isinstance(data, dict):
            raise ConfigError("configuration root must be a mapping")
        return _build(cls, data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # -- environment overrides -------------------------------------------------

    def apply_environment(self, environ: dict[str, str]) -> None:
        mapping: list[tuple[str, Any, str, type]] = [
            ("AIRVIS_WORKSPACE", self, "workspace", str),
            ("AIRVIS_ROUTING_STRATEGY", self.routing, "strategy", str),
            ("AIRVIS_PROVIDER", self.providers, "default", str),
            ("AIRVIS_FALLBACK_PROVIDER", self.providers, "fallbacks", list),
            ("AIRVIS_PROVIDER_TIMEOUT", self.providers, "request_timeout", float),
            ("AIRVIS_MAX_CONCURRENCY", self.workflow, "max_concurrency", int),
            ("AIRVIS_TASK_TIMEOUT", self.workflow, "task_timeout", float),
            ("AIRVIS_MAX_RETRIES", self.repair, "max_retries", int),
            ("AIRVIS_AUTO_APPROVE_MAX_RISK", self.security, "auto_approve_max_risk", str),
            ("AIRVIS_HIGH_RISK_POLICY", self.security, "default_high_risk_policy", str),
            ("AIRVIS_REVIEW_ENABLED", self.review, "enabled", bool),
            ("AIRVIS_REVIEW_MIN_SCORE", self.review, "min_score", float),
            ("AIRVIS_STATE_PATH", self.state, "path", str),
            ("AIRVIS_PERSIST", self.state, "enabled", bool),
            ("AIRVIS_MCP_ENABLED", self.mcp, "enabled", bool),
            ("OPENCLAW_CLI", self.backends, "openclaw_command", str),
            ("HERMES_CLI", self.backends, "hermes_command", str),
        ]
        for name, target, attribute, kind in mapping:
            raw = environ.get(name)
            if raw is None or raw.strip() == "":
                continue
            setattr(target, attribute, _coerce(raw.strip(), kind))
        # Legacy privacy switch used by the V4 ModelRouter.
        privacy = environ.get("AIRVIS_PRIVACY_MODE", "").strip().upper()
        if privacy in {"LOCAL", "LOCAL ONLY", "LOCAL_ONLY"}:
            self.routing.strategy = RoutingStrategy.LOCAL_ONLY.value
        enabled = environ.get("AIRVIS_BACKENDS", "").strip()
        if enabled:
            self.backends.enabled = [item.strip() for item in enabled.split(",") if item.strip()]


# --- helpers -----------------------------------------------------------------


def _coerce(raw: str, kind: type) -> Any:
    if kind is bool:
        return raw.lower() in {"1", "true", "yes", "on"}
    if kind is int:
        return int(float(raw))
    if kind is float:
        return float(raw)
    if kind is list:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _resolve_config_path(
    path: str | Path | None, environ: dict[str, str], search_from: str | Path | None
) -> Path | None:
    if path is not None:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"configuration file not found: {candidate}")
        return candidate
    from_env = environ.get("AIRVIS_CONFIG", "").strip()
    if from_env:
        candidate = Path(from_env).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"AIRVIS_CONFIG points at a missing file: {candidate}")
        return candidate

    # An explicit workspace is an isolation boundary. Do not silently inherit
    # ~/.airvis configuration from an unrelated user workspace. The home config
    # remains available for the normal no-workspace invocation.
    if search_from is not None:
        roots = [Path(search_from).expanduser()]
    else:
        roots = [Path.cwd(), Path.home() / ".airvis"]

    for root in roots:
        for name in CONFIG_FILENAMES:
            candidate = root / name
            if candidate.is_file():
                return candidate
    return None


def _read_config_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ConfigError(
                f"{path} is YAML but PyYAML is not installed; install pyyaml or use JSON"
            ) from exc
        loaded = yaml.safe_load(text) or {}
    else:
        try:
            loaded = json.loads(text or "{}")
        except ValueError as exc:
            raise ConfigError(f"invalid JSON configuration in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"configuration root of {path} must be a mapping")
    return loaded


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build(cls: type, data: dict[str, Any]) -> Any:
    kwargs: dict[str, Any] = {}
    known = {item.name: item for item in fields(cls)}
    for key, value in data.items():
        target = known.get(key)
        if target is None:
            raise ConfigError(f"unknown configuration key '{key}' for {cls.__name__}")
        kwargs[key] = _build_value(target.type, value, cls.__name__, key)
    return cls(**kwargs)


def _build_value(annotation: Any, value: Any, owner: str, key: str) -> Any:
    if isinstance(annotation, str):
        annotation = _ANNOTATIONS.get(annotation, annotation)
    if isinstance(annotation, type) and is_dataclass(annotation) and isinstance(value, dict):
        return _build(annotation, value)
    if annotation is MCPConfig and isinstance(value, dict):
        return _build(MCPConfig, value)
    if key == "servers" and isinstance(value, list):
        return [item if isinstance(item, MCPServerConfig) else _build(MCPServerConfig, item) for item in value]
    if isinstance(value, dict) and annotation in (dict, Any):
        return value
    return value


_ANNOTATIONS: dict[str, Any] = {
    "RoutingConfig": RoutingConfig,
    "AgentsConfig": AgentsConfig,
    "ProvidersConfig": ProvidersConfig,
    "BackendsConfig": BackendsConfig,
    "SecurityConfig": SecurityConfig,
    "RepairConfig": RepairConfig,
    "WorkflowConfig": WorkflowConfig,
    "ReviewConfig": ReviewConfig,
    "ContextConfig": ContextConfig,
    "MCPConfig": MCPConfig,
    "MCPServerConfig": MCPServerConfig,
    "StateConfig": StateConfig,
}


__all__ = [
    "STRATEGY_WEIGHTS",
    "AgentsConfig",
    "AirvisConfig",
    "BackendsConfig",
    "ContextConfig",
    "MCPConfig",
    "MCPServerConfig",
    "ProvidersConfig",
    "RepairConfig",
    "ReviewConfig",
    "RoutingConfig",
    "RoutingStrategy",
    "SecurityConfig",
    "StateConfig",
    "WorkflowConfig",
]
