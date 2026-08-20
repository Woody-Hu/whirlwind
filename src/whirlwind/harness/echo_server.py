"""Echo harness: the reference conformance implementation of the Whirlwind wire protocol.

A threaded stdlib program (run as `python -m whirlwind.harness.echo_server`) that
implements the dsh JSON-RPC stdio subset. It is the protocol baseline every
integration test drives through a real subprocess — deliberately not a mock:
turns produce real events, and with ECHO_LLM_URL set it performs a real
OpenAI-style chat completion call through that endpoint (used to exercise the
LLM relay path without touching the internet).

No asyncio on purpose: the harness child's interpreter boot sits on the
sandbox cold-start critical path, and `import asyncio` alone measured ~80ms
on FUSE-backed storage (concurrent.futures -> logging -> traceback chain).
A thread-per-turn model keeps every behaviour of the previous async version:
the session/prompt response is still written before any turn event (the turn
thread blocks on an Event until the response is flushed), chunk pacing is a
plain sleep, and `/run` uses a blocking subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
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

    # ------------------------------------------------------------------ loop

    def run(self) -> None:
        """Main thread reads stdin line by line; each turn runs on its own
        thread so overlapping session/prompt calls stay concurrent."""
        for raw in sys.stdin.buffer:
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            self._handle(message)

    def _handle(self, message: dict) -> None:
        msg_id = message.get("id")
        method = message.get("method")
        if isinstance(msg_id, (str, int)) and isinstance(method, str):
            try:
                result, after_write = self._call(method, message.get("params") or {})
            except Exception as exc:  # protocol errors must answer, not crash
                self._send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(exc)}})
                return
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})
            if after_write is not None:
                after_write.set()  # turn events may start streaming now
        elif isinstance(method, str):
            # notifications from the driver: none defined in M1
            return

    # ---------------------------------------------------------------- calls

    def _call(self, method: str, params: dict) -> tuple[dict, threading.Event | None]:
        """Returns (result, gate): the gate is set after the response is sent,
        releasing the turn thread that was spawned alongside it."""
        if method == "initialize":
            return {"serverInfo": {"name": "echo-harness", "version": "0.1.0"}}, None
        if method == "session/prompt":
            session_id = params.get("sessionId") or f"session-{uuid.uuid4().hex}"
            blocks = params.get("contentBlocks") or []
            message_id = f"msg_{uuid.uuid4().hex[:8]}"
            gate = threading.Event()
            threading.Thread(
                target=self._run_turn, args=(session_id, blocks, message_id, gate), daemon=True
            ).start()
            return {"messageId": message_id}, gate
        if method == "shutdown":
            return {}, None
        raise ValueError(f"unknown method {method}")

    def _run_turn(self, session_id: str, blocks: list, message_id: str, gate: threading.Event) -> None:
        gate.wait()  # the session/prompt response goes out first
        text = "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        self._sessions.setdefault(session_id, {"messages": []})["messages"].append(text)
        self._status(session_id, "running")
        self._emit(session_id, "turn/start", {"input_ref": message_id})
        # a turn must always end with turn/end — failures surface as error + end(error)
        try:
            reply = self._reply_for(text)
        except Exception as exc:
            self._emit(session_id, "error", {"message": f"reply failed: {exc}"})
            self._emit(
                session_id,
                "turn/end",
                {"reason": {"kind": "error", "message": str(exc)}, "usage": {"input": 0, "output": 0}},
            )
            self._status(session_id, "idle")
            return
        if self._chunk_mode == "chars":
            for i in range(0, len(reply), 8):
                self._emit(session_id, "assistant/chunk", {"delta": reply[i : i + 8]})
                time.sleep(0.005)
        self._emit(
            session_id,
            "assistant/message",
            {"message": {"content": [{"type": "text", "text": reply}]}},
        )
        if text.startswith("/run "):
            command = text[len("/run ") :]
            self._emit(
                session_id,
                "tool/call",
                {"tool": "shell.exec", "seam": "shell.v1", "args": {"command": command}},
            )
            proc = subprocess.run(
                command,
                shell=True,
                cwd=os.environ.get("ECHO_CWD", os.getcwd()),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self._emit(
                session_id,
                "tool/result",
                {"tool": "shell.exec", "exit_code": proc.returncode, "output": proc.stdout.decode(errors="replace")},
            )
        self._emit(
            session_id,
            "turn/end",
            {"reason": {"kind": "completed"}, "usage": {"input": 0, "output": 0}},
        )
        self._status(session_id, "idle")

    def _reply_for(self, text: str) -> str:
        if not self._llm_url:
            return f"echo: {text}"
        # real OpenAI-style call through ECHO_LLM_URL (the sidecar relay hop).
        # urllib.request is imported lazily: it drags in email/http/ssl (~40ms),
        # and the no-LLM boot path must not pay it.
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

    # ---------------------------------------------------------------- output

    def _emit(self, session_id: str, type_: str, data: dict) -> None:
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        event = {"type": type_, "seq": seq, "time": _now_ms(), "data": data}
        self._notify(
            "session.event",
            {"sessionId": session_id, "event": event},
        )

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
    EchoHarness().run()


if __name__ == "__main__":
    main()
