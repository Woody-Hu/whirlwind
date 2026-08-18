"""MCP Gateway: the fallback Consumer face (architecture 8.2 / ADR D7).

Exposes an AgentVersion's seam capabilities as MCP tools over the Streamable
HTTP subset (POST JSON-RPC: initialize / tools/list / tools/call). Seams whose
rendered consumers include `harness: "*"` are listed; calls execute through a
WorkspaceToolExecutor — real filesystem / subprocess / HTTP work scoped to the
session workspace when a live session is named, else a per-version scratch
root. The MCP face never enters the scheduling hot path.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from argus.core import AgentVersion
from argus.core.errors import ArgusError, NotFound
from argus.seam.model import SeamRegistry
from argus.storage.providers import MetadataStore

MCP_PROTOCOL_VERSION = "2024-11-05"

_JSONRPC_ERRORS = {
    "parse": (-32700, "Parse error"),
    "method": (-32601, "Method not found"),
    "params": (-32602, "Invalid params"),
    "internal": (-32603, "Internal error"),
}


def jsonrpc_response(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def jsonrpc_error(msg_id: Any, kind: str, message: str) -> dict[str, Any]:
    code, default = _JSONRPC_ERRORS[kind]
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message or default}}


class McpError(ArgusError):
    code = "argus/mcp"


# ------------------------------------------------------------------ executor


@dataclass
class ToolOutcome:
    text: str
    is_error: bool = False


class WorkspaceToolExecutor:
    """Host-side execution of builtin seam tools, confined to a workspace root.

    Every tool does real work (real subprocess, real file IO, real HTTP); the
    only policy is confinement: paths stay under the root, shell runs with
    cwd=root, memory lives under .argus/.
    """

    def __init__(self, store: MetadataStore, data_dir: Path, http: httpx.AsyncClient | None = None) -> None:
        self.store = store
        self.data_dir = data_dir
        self._http = http or httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def resolve_root(self, version: AgentVersion, session_id: str | None) -> Path:
        if session_id:
            session = await self.store.get_session(session_id)
            if session is None:
                raise NotFound(f"session {session_id}")
            if session.bound_sandbox_id:
                sandbox = await self.store.get_sandbox(session.bound_sandbox_id)
                if sandbox is not None and sandbox.workspace:
                    return Path(sandbox.workspace)
            raise McpError(f"session {session_id} has no live sandbox workspace")
        root = self.data_dir / "mcp" / version.id / "ws"
        root.mkdir(parents=True, exist_ok=True)
        return root

    async def execute(self, tool: str, args: dict[str, Any], root: Path) -> ToolOutcome:
        handlers = {
            "fs_read": self._fs_read,
            "fs_write": self._fs_write,
            "shell_exec": self._shell_exec,
            "web_fetch": self._web_fetch,
            "memory_save": self._memory_save,
            "memory_load": self._memory_load,
        }
        handler = handlers.get(tool)
        if handler is None:
            return ToolOutcome(f"unknown tool {tool!r}", is_error=True)
        try:
            return await handler(args, root)
        except ArgusError as exc:
            return ToolOutcome(str(exc), is_error=True)
        except Exception as exc:  # tool failures surface as MCP errors, not 500s
            return ToolOutcome(f"{type(exc).__name__}: {exc}", is_error=True)

    # -- fs.v1 ----------------------------------------------------------

    async def _fs_read(self, args: dict, root: Path) -> ToolOutcome:
        target = _confine(root, str(args.get("path", "")))
        if not target.is_file():
            return ToolOutcome(f"no such file: {args.get('path')}", is_error=True)
        return ToolOutcome(target.read_text(errors="replace")[:200_000])

    async def _fs_write(self, args: dict, root: Path) -> ToolOutcome:
        target = _confine(root, str(args.get("path", "")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(args.get("content", "")))
        return ToolOutcome(f"wrote {len(str(args.get('content', '')))} bytes to {args.get('path')}")

    # -- shell.v1 ---------------------------------------------------------

    async def _shell_exec(self, args: dict, root: Path) -> ToolOutcome:
        command = str(args.get("command", ""))
        if not command:
            return ToolOutcome("empty command", is_error=True)
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        text = out.decode(errors="replace")
        return ToolOutcome(f"[exit {proc.returncode}]\n{text}")

    # -- web.v1 -----------------------------------------------------------

    async def _web_fetch(self, args: dict, root: Path) -> ToolOutcome:
        url = str(args.get("url", ""))
        response = await self._http.get(url)
        content_type = response.headers.get("content-type", "")
        body = response.text if "text" in content_type or "json" in content_type else f"({content_type}, {len(response.content)} bytes)"
        return ToolOutcome(f"HTTP {response.status_code}\n{body[:100_000]}")

    # -- memory.v1 ----------------------------------------------------------

    async def _memory_save(self, args: dict, root: Path) -> ToolOutcome:
        store_file = root / ".argus" / "memory.json"
        store_file.parent.mkdir(parents=True, exist_ok=True)
        notes: dict[str, str] = {}
        if store_file.is_file():
            notes = json.loads(store_file.read_text())
        notes[str(args.get("key", ""))] = str(args.get("value", ""))
        store_file.write_text(json.dumps(notes))
        return ToolOutcome(f"saved {args.get('key')!r} ({len(notes)} notes)")

    async def _memory_load(self, args: dict, root: Path) -> ToolOutcome:
        store_file = root / ".argus" / "memory.json"
        if not store_file.is_file():
            return ToolOutcome("(no notes)")
        return ToolOutcome(store_file.read_text()[:100_000])


def _confine(root: Path, relative: str) -> Path:
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise McpError(f"path {relative!r} must be relative and stay inside the workspace")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise McpError(f"path {relative!r} escapes the workspace")
    return target


# ------------------------------------------------------------------ gateway


class McpGateway:
    """JSON-RPC dispatch for `/mcp/{version_id}` (Streamable HTTP subset)."""

    def __init__(
        self,
        store: MetadataStore,
        renderer: Any,  # SeamRenderer (kept loose: gateway needs render_bindings + registry)
        executor: WorkspaceToolExecutor,
    ) -> None:
        self.store = store
        self.renderer = renderer
        self.registry = getattr(renderer, "registry", None) or SeamRegistry()
        self.executor = executor

    def list_tools(self, version: AgentVersion) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for binding in self.renderer.render_bindings(version):
            if not any(c.harness == "*" for c in binding.consumers):
                continue
            seam = self.registry.seam(binding.seam)
            for tool in seam.tools:
                tools.append(
                    {
                        "name": tool.name,
                        "description": f"[{seam.id}:{binding.provider}] {tool.description}",
                        "inputSchema": tool.input_schema,
                    }
                )
        return tools

    async def call_tool(
        self,
        version: AgentVersion,
        name: str,
        arguments: dict[str, Any] | None,
        session_id: str | None,
    ) -> dict[str, Any]:
        known = {t["name"] for t in self.list_tools(version)}
        if name not in known:
            return {
                "content": [{"type": "text", "text": f"tool {name!r} is not exposed for this version"}],
                "isError": True,
            }
        try:
            root = await self.executor.resolve_root(version, session_id)
        except ArgusError as exc:  # execution-context problems are tool errors, not protocol errors
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        outcome = await self.executor.execute(name, arguments or {}, root)
        return {
            "content": [{"type": "text", "text": outcome.text}],
            "isError": outcome.is_error,
        }

    async def handle(self, version_id: str, body: bytes) -> dict[str, Any]:
        """One JSON-RPC message in, one message out (M1 subset: no batching)."""
        try:
            message = json.loads(body)
        except json.JSONDecodeError:
            return jsonrpc_error(None, "parse", "malformed JSON")
        if not isinstance(message, dict):
            return jsonrpc_error(None, "parse", "message must be an object")
        msg_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return jsonrpc_error(msg_id, "parse", "missing method")
        params = message.get("params") or {}
        try:
            return await self._dispatch(version_id, msg_id, method, params)
        except NotFound as exc:
            return jsonrpc_error(msg_id, "params", str(exc))
        except ArgusError as exc:
            return jsonrpc_error(msg_id, "internal", str(exc))

    async def _dispatch(self, version_id: str, msg_id: Any, method: str, params: dict) -> dict[str, Any]:
        version = await self.store.get_version(version_id)
        if version is None:
            raise NotFound(f"agent version {version_id}")
        if method == "initialize":
            return jsonrpc_response(
                msg_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "argus-mcp", "version": "0.1.0"},
                },
            )
        if method == "tools/list":
            return jsonrpc_response(msg_id, {"tools": self.list_tools(version)})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return jsonrpc_error(msg_id, "params", "tools/call requires name")
            session_id = params.get("_session") or params.get("sessionId")
            result = await self.call_tool(version, name, params.get("arguments"), session_id)
            return jsonrpc_response(msg_id, result)
        return jsonrpc_error(msg_id, "method", f"unsupported method {method!r}")
