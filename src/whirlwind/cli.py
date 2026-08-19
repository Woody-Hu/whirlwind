"""`whirlwind` command line client (ADR D8).

Pure httpx against a running gateway (`WHIRLWIND_URL`, default
http://127.0.0.1:8410). `serve` is the only local command — it assembles the
WhirlwindRuntime in-process and hands its app to uvicorn (the app lifespan runs
runtime.start/stop).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8410"


def _base_url(args: argparse.Namespace) -> str:
    return getattr(args, "url", None) or os.environ.get("WHIRLWIND_URL", DEFAULT_URL)


def _client(args: argparse.Namespace) -> httpx.Client:
    return httpx.Client(base_url=_base_url(args), timeout=120.0)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _check(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        try:
            body = response.json()
            detail = body.get("error", {}).get("message") or body.get("error", {}).get("code")
        except Exception:
            detail = response.text
        _die(f"HTTP {response.status_code}: {detail}")
    return response.json()


# ------------------------------------------------------------------ commands


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from whirlwind.runtime import WhirlwindRuntime, RuntimeConfig

    runtime = WhirlwindRuntime(
        RuntimeConfig(
            data_dir=_path(args.data_dir),
            repo_root=_path(args.repo_root) if args.repo_root else None,
            api_key_env=args.api_key_env,
            llm_upstream=args.llm_upstream,
            metadata_backend=args.metadata_backend,
            postgres_dsn=args.postgres_dsn,
            kv_backend=args.kv_backend,
            redis_url=args.redis_url,
        )
    )
    uvicorn.run(runtime.app, host=args.host, port=args.port, log_level="info")


def _path(value: str) -> "Any":
    from pathlib import Path

    return Path(value).expanduser().resolve()


def cmd_image_build(args: argparse.Namespace) -> None:
    with _client(args) as client:
        _print_json(_check(client.post(f"/images/{args.name}/build")))


def cmd_agent_create(args: argparse.Namespace) -> None:
    seam_bindings = []
    for spec in args.seam or []:
        seam, _, provider = spec.partition("=")
        if not provider:
            _die(f"--seam expects seam=provider, got {spec!r}")
        seam_bindings.append({"seam": seam, "provider": provider})
    skills = [{"name": name, "version": version or "1.0.0"} for name, _, version in (s.partition("=") for s in (args.skill or []))]
    model = {key: value for key, _, value in (m.partition("=") for m in (args.model or []))}
    payload = {
        "name": args.name,
        "version": {
            "version": args.agent_version,
            "harness": args.harness,
            "image_ref": args.image,
            "seam_bindings": seam_bindings,
            "skill_refs": skills,
            "model_config_decl": model,
        },
    }
    with _client(args) as client:
        _print_json(_check(client.post("/agents", json=payload)))


def cmd_agent_list(args: argparse.Namespace) -> None:
    with _client(args) as client:
        for agent in _check(client.get("/agents")):
            print(f"{agent['id']}\t{agent['name']}\tdefault={agent.get('default_version_id')}")


def cmd_session_create(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {}
    if args.agent.startswith(("agt_", "ver_")):
        payload["agent_id"] = args.agent
    else:
        payload["agent_name"] = args.agent
    if args.version_id:
        payload["version_id"] = args.version_id
    with _client(args) as client:
        session = _check(client.post("/sessions", json=payload))
        print(session["id"])


def cmd_session_send(args: argparse.Namespace) -> None:
    with _client(args) as client:
        result = _check(client.post(f"/sessions/{args.session}/turns", json={"text": args.text}))
        print(f"message: {result['message_id']}")
    if args.stream:
        _stream_events(_base_url(args), args.session)


def cmd_session_lifecycle(args: argparse.Namespace) -> None:
    with _client(args) as client:
        session = _check(client.post(f"/sessions/{args.session}/{args.action}"))
    print(f"session {session['id']}: {session['status']}")


def _stream_events(base_url: str, session_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=None) as client:
        with client.stream("GET", f"/sessions/{session_id}/stream") as response:
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                kind = event.get("type", "")
                data = event.get("data") or {}
                if kind == "assistant/chunk":
                    print(data.get("delta", ""), end="", flush=True)
                elif kind == "assistant/message":
                    content = data.get("message", {}).get("content", [])
                    text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                    print(f"\n[assistant] {text}", flush=True)
                elif kind == "tool/call":
                    print(f"\n[tool] {data.get('tool')} {json.dumps(data.get('args', {}))}", flush=True)
                elif kind == "tool/result":
                    print(f"[result exit={data.get('exit_code')}] {str(data.get('output', ''))[:500]}", flush=True)
                elif kind == "turn/end":
                    reason = (data.get("reason") or {}).get("kind", "completed")
                    print()
                    if reason != "completed":
                        _die(f"turn ended with reason={reason}")
                    return
                else:
                    print(f"[{kind}] {json.dumps(data, ensure_ascii=False)[:300]}", flush=True)


def cmd_session_events(args: argparse.Namespace) -> None:
    with _client(args) as client:
        events = _check(client.get(f"/sessions/{args.session}/events", params={"from_seq": args.from_seq}))
        for event in events:
            print(f"{event['seq']:>6}  {event['type']:<20} {json.dumps(event.get('data', {}), ensure_ascii=False)[:200]}")


def cmd_cron_add(args: argparse.Namespace) -> None:
    payload = {
        "agent_id": args.agent,
        "schedule": args.schedule,
        "input_template": args.input,
        "session_policy": "reuse" if args.reuse else "fresh",
    }
    if args.session:
        payload["session_id"] = args.session
    with _client(args) as client:
        _print_json(_check(client.post("/crons", json=payload)))


def cmd_cron_list(args: argparse.Namespace) -> None:
    with _client(args) as client:
        for job in _check(client.get("/crons")):
            state = "enabled" if job.get("enabled") else "disabled"
            print(f"{job['id']}\t{state}\t{job['schedule']}\t{job['agent_id']}")


def cmd_cron_trigger(args: argparse.Namespace) -> None:
    with _client(args) as client:
        _print_json(_check(client.post(f"/crons/{args.cron_id}/trigger")))


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="whirlwind", description="Whirlwind agent runtime client")
    parser.add_argument("--url", help=f"gateway base URL (default: $WHIRLWIND_URL or {DEFAULT_URL})")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the all-in-one runtime + gateway")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8410)
    serve.add_argument("--data-dir", default=".whirlwind")
    serve.add_argument("--repo-root", default=None, help="repo image builds install whirlwind from (default: auto)")
    serve.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    serve.add_argument("--llm-upstream", default="https://api.deepseek.com")
    serve.add_argument("--metadata-backend", default="memory", choices=["memory", "postgres"],
                       help="metadata store backend (default: memory)")
    serve.add_argument("--postgres-dsn", default=None, help="postgresql://user:pass@host:port/db (with --metadata-backend postgres)")
    serve.add_argument("--kv-backend", default="memory", choices=["memory", "redis"],
                       help="hot-state KV backend (default: memory)")
    serve.add_argument("--redis-url", default=None, help="redis://[:pass@]host:port/db (with --kv-backend redis)")
    serve.set_defaults(func=cmd_serve)

    image = sub.add_parser("image")
    image_sub = image.add_subparsers(dest="image_command", required=True)
    build = image_sub.add_parser("build")
    build.add_argument("name", choices=["echo", "dsh"])
    build.set_defaults(func=cmd_image_build)

    agent = sub.add_parser("agent")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    create = agent_sub.add_parser("create")
    create.add_argument("name")
    create.add_argument("--harness", required=True)
    create.add_argument("--image", required=True)
    create.add_argument("--agent-version", default="1.0.0")
    create.add_argument("--model", action="append", help="model_config_decl entry key=value (repeatable)")
    create.add_argument("--seam", action="append", help="seam=provider binding (repeatable)")
    create.add_argument("--skill", action="append", help="name=version skill ref (repeatable)")
    create.set_defaults(func=cmd_agent_create)
    listing = agent_sub.add_parser("list")
    listing.set_defaults(func=cmd_agent_list)

    session = sub.add_parser("session")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    sc = session_sub.add_parser("create")
    sc.add_argument("agent", help="agent id or name")
    sc.add_argument("--version-id")
    sc.set_defaults(func=cmd_session_create)
    ss = session_sub.add_parser("send")
    ss.add_argument("session")
    ss.add_argument("text")
    ss.add_argument("--stream", action="store_true", help="stream events until turn/end")
    ss.set_defaults(func=cmd_session_send)
    se = session_sub.add_parser("events")
    se.add_argument("session")
    se.add_argument("--from-seq", type=int, default=0)
    se.set_defaults(func=cmd_session_events)
    for action in ("suspend", "resume", "close"):
        sl = session_sub.add_parser(action)
        sl.add_argument("session")
        sl.set_defaults(func=cmd_session_lifecycle, action=action)

    cron = sub.add_parser("cron")
    cron_sub = cron.add_subparsers(dest="cron_command", required=True)
    ca = cron_sub.add_parser("add")
    ca.add_argument("agent", help="agent id")
    ca.add_argument("--schedule", required=True, help="5-field cron expression")
    ca.add_argument("--input", required=True, help="turn input text")
    ca.add_argument("--reuse", action="store_true", help="reuse a live session instead of fresh")
    ca.add_argument("--session", help="session id to reuse (with --reuse)")
    ca.set_defaults(func=cmd_cron_add)
    cl = cron_sub.add_parser("list")
    cl.set_defaults(func=cmd_cron_list)
    ct = cron_sub.add_parser("trigger")
    ct.add_argument("cron_id")
    ct.set_defaults(func=cmd_cron_trigger)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
