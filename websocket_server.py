from __future__ import annotations

import asyncio
import json
import os

from airvis.runtime import AgentRuntime


async def serve(runtime: AgentRuntime | None = None, host: str | None = None, port: int | None = None) -> None:
    import websockets

    active_runtime = runtime or AgentRuntime()
    bind_host = host or os.environ.get("AIRVIS_HOST", "127.0.0.1")
    bind_port = port or int(os.environ.get("AIRVIS_WS_PORT", "8766"))
    token = os.environ.get("AIRVIS_API_TOKEN", "").strip()
    protected = bool(token) or bind_host not in {"127.0.0.1", "localhost", "::1"}

    async def handler(websocket):
        if protected and websocket.request.headers.get("Authorization") != f"Bearer {token}":
            await websocket.close(code=4401, reason="authentication required")
            return
        await websocket.send(json.dumps({"event": "assistant.state", "data": active_runtime.status()}))
        async for raw in websocket:
            try:
                payload = json.loads(raw)
                message = str(payload.get("message", "")).strip()
                if not message:
                    await websocket.send(json.dumps({"event": "error", "error": "message is required"}))
                    continue
                await websocket.send(json.dumps({"event": "assistant.state", "data": {"state": "thinking"}}))
                response = await asyncio.to_thread(active_runtime.run, message)
                await websocket.send(json.dumps({"event": "assistant.message", "data": {"response": response}}))
                await websocket.send(json.dumps({"event": "assistant.state", "data": active_runtime.status()}))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                await websocket.send(json.dumps({"event": "error", "error": str(exc)}))

    async with websockets.serve(handler, bind_host, bind_port):
        print(f"AIRVIS WebSocket: ws://{bind_host}:{bind_port}")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(serve())
