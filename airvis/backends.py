from __future__ import annotations

from dataclasses import dataclass, field
import subprocess


@dataclass
class CLIBackend:
    id: str
    command: str
    timeout: int = 120
    capabilities: set[str] = field(default_factory=lambda: {"chat", "tools", "automation"})

    def chat(self, messages, tools) -> str:
        prompt = messages[-1]["content"] if messages else ""
        result = subprocess.run([self.command, "agent", "--message", prompt], capture_output=True, text=True, timeout=self.timeout, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"{self.id} failed")
        return result.stdout.strip()


class OpenClawBackend(CLIBackend):
    def __init__(self, command: str = "openclaw") -> None:
        super().__init__("openclaw", command)


class HermesBackend(CLIBackend):
    def __init__(self, command: str = "hermes") -> None:
        super().__init__("hermes", command)
