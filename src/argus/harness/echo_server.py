"""Echo harness: the reference conformance implementation of the Argus wire protocol.

A real asyncio program (run as `python -m argus.harness.echo_server`) that
implements the dsh JSON-RPC stdio subset. It is the protocol baseline every
integration test drives through a real subprocess — deliberately not a mock:
turns produce real events, and with ECHO_LLM_URL set it performs a real
OpenAI-style chat completion call through that endpoint (used to exercise the
LLM relay path without touching the internet).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid


def _now_ms() -> int:
    return int(time.time() * 1000)


class EchoHarness:
    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._seq: dict[str, int] = {}
        self._llm_url = os.environ.get("ECHO_LLM_URL", "")
        self._chunk_mode = os.environ.get("ECHO_CHUNK_MODE", "chars")  # chars | none
        self._write_lock = threading.Lock()

    async def run(self) -> None:
        """Read stdin on a worker thread, bridge lines into the event loop.

        Threaded stdin + locked stdout is deliberately portable (Linux/macOS)
        and avoids asyncio pipe-transport edge cases under subprocess stdio.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _stdin_thread() -> None:
            try:
                for raw in sys.stdin.buffer:
                    loop.call_soon_threadsafe(queue.put_nowait, raw.decode("utf-8", errors="replace"))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=_stdin_thread, daemon=True).start()
        while True:
            line = await queue.get()
            if line is None:
                break
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            await self._handle(message)

    async def _handle(self, message: dict) -> None:
        msg_id = message.get("id")
        method = message.get("method")
        if isinstance(msg_id, (str, int)) and isinstance(method, str):
            try:
                result = await self._call(method, message.get("params") or {})
            except Exception as exc:  # protocol errors must answer, not crash
                self._send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(exc)}})
                return
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif isinstance(method, str):
            # notifications from the driver: none defined in M1
            return

    async def _call(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return {"serverInfo": {"name": "echo-harness", "version": "0.1.0"}}
        if method == "session/prompt":
            session_id = params.get("sessionId") or f"session-{uuid.uuid4().hex}"
            blocks = params.get("contentBlocks") or []
            message_id = f"msg_{uuid.uuid4().hex[:8]}"
            # defer the turn so the response returns before events stream out
            asyncio.get_running_loop().create_task(self._run_turn(session_id, blocks, message_id))
            return {"messageId": message_id}
        if method == "shutdown":
            return {}
        raise ValueError(f"unknown method {method}")

    async def _run_turn(self, session_id: str, blocks: list, message_id: str) -> None:
        await asyncio.sleep(0)  # yield so session/prompt response is written first
        text = "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        reply = await self._reply_for(text)
        self._sessions.setdefault(session_id, {"messages": []})["messages"].append(text)
        self._status(session_id, "busy")
        await self._emit(session_id, "turn/start", {"input_ref": message_id})
        if self._chunk_mode == "chars":
            for i in range(0, len(reply), 8):
                await self._emit(session_id, "assistant/chunk", {"delta": reply[i : i + 8]})
                await asyncio.sleep(0.005)
        await self._emit(
            session_id,
            "assistant/message",
            {"message": {"content": [{"type": "text", "text": reply}]}},
        )
        if text.startswith("/run "):
            command = text[len("/run ") :]
            await self._emit(
                session_id,
                "tool/call",
                {"tool": "shell.exec", "seam": "shell.v1", "args": {"command": command}},
            )
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=os.environ.get("ECHO_CWD", os.getcwd()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            await self._emit(
                session_id,
                "tool/result",
                {"tool": "shell.exec", "exit_code": proc.returncode, "output": out.decode(errors="replace")},
            )
        await self._emit(
            session_id,
            "turn/end",
            {"reason": {"kind": "completed"}, "usage": {"input": 0, "output": 0}},
        )
        self._status(session_id, "idle")

    async def _reply_for(self, text: str) -> str:
        if not self._llm_url:
            return f"echo: {text}"
        # real OpenAI-style call through ECHO_LLM_URL (the sidecar relay hop)
        import urllib.request

        body = json.dumps(
            {
                "model": os.environ.get("ECHO_LLM_MODEL", "echo-model"),
                "messages": [{"role": "user", "content": text}],
            }
        ).encode()
        req = urllib.request.Request(
            self._llm_url.rstrip("/") + "/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
        return payload["choices"][0]["message"]["content"]

    async def _emit(self, session_id: str, type_: str, data: dict) -> None:
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        event = {"type": type_, "seq": seq, "time": _now_ms(), "data": data}
        self._notify(
            "session.event",
            {"sessionId": session_id, "event": event},
        )
        await asyncio.sleep(0)

    def _status(self, session_id: str, status: str) -> None:
        self._notify("session.status", {"sessionId": session_id, "status": status})

    def _send(self, message: dict) -> None:
        line = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        with self._write_lock:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    def _notify(self, method: str, payload: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": payload})


def main() -> None:
    harness = EchoHarness()
    try:
        asyncio.run(harness.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
