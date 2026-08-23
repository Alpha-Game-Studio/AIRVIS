import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from airvis.provider_manager import ProviderManager
from airvis.runtime import AgentRuntime, PermissionError


class NativeRuntimeTests(unittest.TestCase):
    def test_provider_fallback(self) -> None:
        class Broken:
            id = "broken"
            capabilities = {"chat"}

            def chat(self, messages, tools):
                raise RuntimeError("offline")

        class Working:
            id = "working"
            capabilities = {"chat"}

            def chat(self, messages, tools):
                return "fallback ok"

        self.assertEqual(ProviderManager([Broken(), Working()]).chat([], []), "fallback ok")

    def test_mock_chat_and_memory(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = AgentRuntime(Path(directory), memory_path=Path(directory) / "memory.db", session_path=Path(directory) / "sessions.json")
            self.assertIn("Mock Provider", runtime.run("hello"))
            self.assertEqual(runtime.run("이거 기억해: 내가 쓰는 에디터는 Cursor"), "기억해두었습니다.")
            self.assertEqual(len(runtime.memory.list()), 1)

    def test_tools_are_workspace_scoped_and_confirmed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "report.txt").write_text("ok", encoding="utf-8")
            runtime = AgentRuntime(root, memory_path=root / "memory.db", session_path=root / "sessions.json")
            self.assertEqual(runtime.execute_tool("filesystem.search", {"pattern": "*.txt"}), ["report.txt"])
            with self.assertRaises(PermissionError):
                runtime.execute_tool("terminal.execute", {"command": "pwd"})
            with self.assertRaises(PermissionError):
                runtime.execute_tool("filesystem.read", {"path": "../outside.txt"})


if __name__ == "__main__":
    unittest.main()
