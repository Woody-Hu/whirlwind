"""`whirlwind` command line client (ADR D8).

Pure httpx against a running gateway (`WHIRLWIND_URL`, default
http://127.0.0.1:8410). `serve` is the only local command — it assembles the
WhirlwindRuntime in-process and hands its app to uvicorn (the app lifespan runs
runtime.start/stop).

Configuration (ADR-0009): `serve` resolves its settings through
`whirlwind.config.load_settings` — code defaults < whirlwind.toml <
`WHIRLWIND_*` env < explicit CLI flags. The flags below carry `None` sentinels,
so only explicitly-given flags override; defaults live exactly once in the
loader. `whirlwind config show` prints the fully resolved configuration.
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

    from whirlwind.config import ConfigError, load_settings
    from whirlwind.runtime import WhirlwindRuntime

    try:
        settings = load_settings(
            config_path=args.config,
            cli={
                "server.host": args.host,
                "server.port": args.port,
                "runtime.data_dir": args.data_dir,
                "runtime.repo_root": args.repo_root,
                "runtime.api_key_env": args.api_key_env,
                "runtime.llm_upstream": args.llm_upstream,
                "storage.metadata_backend": args.metadata_backend,
                "storage.postgres_dsn": args.postgres_dsn,
                "storage.kv_backend": args.kv_backend,
                "storage.redis_url": args.redis_url,
                "sandbox.max_live_sessions": args.max_live_sessions,
                "sandbox.driver": args.driver,
                "sandbox.snapshot_mode": args.snapshot_mode,
                "sandbox.snapshot_chain_max": args.snapshot_chain_max,
            },
        )
    except ConfigError as exc:
        _die(str(exc))
    runtime = WhirlwindRuntime(settings.runtime)
    uvicorn.run(runtime.app, host=settings.server.host, port=settings.server.port, log_level="info")


def cmd_config_show(args: argparse.Namespace) -> None:
    from whirlwind.config import ConfigError, load_settings, render_toml

    try:
        settings = load_settings(config_path=args.config)
    except ConfigError as exc:
        _die(str(exc))
    print(render_toml(settings))


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
    env: dict[str, str] = {}
    for spec in args.env or []:
        name, sep, value = spec.partition("=")
        if not sep:
            # bare NAME: pull the value from this process's environment so the
            # secret never appears in shell history or process listings
            if name not in os.environ:
                _die(f"--env {name}: not set in the local environment")
            value = os.environ[name]
        env[name] = value
    # ADR-0011 D4: with --harness-bundle the gateway derives harness/image_ref
    # from the bundle; explicit flags stay optional (must agree or 422).
    version_payload: dict[str, Any] = {
        "version": args.agent_version,
        "seam_bindings": seam_bindings,
        "skill_refs": skills,
        "model_config_decl": model,
    }
    if args.harness_bundle:
        version_payload["harness_bundle"] = args.harness_bundle
        if args.harness:
            version_payload["harness"] = args.harness
        if args.image:
            version_payload["image_ref"] = args.image
    else:
        if not (args.harness and args.image):
            _die("--harness and --image are required (or pass --harness-bundle)")
        version_payload["harness"] = args.harness
        version_payload["image_ref"] = args.image
    if args.seam_instance:
        version_payload["seam_instances"] = args.seam_instance
    if env:
        version_payload["env"] = env
    payload = {"name": args.name, "version": version_payload}
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
    serve.add_argument("--config", default=None,
                       help="path to whirlwind.toml (default: $WHIRLWIND_CONFIG or ./whirlwind.toml)")
    serve.add_argument("--host", default=None, help="bind host (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=None, help="bind port (default: 8410)")
    serve.add_argument("--data-dir", default=None, help="state directory (default: .whirlwind)")
    serve.add_argument("--repo-root", default=None, help="repo image builds install whirlwind from (default: auto)")
    serve.add_argument("--api-key-env", default=None,
                       help="NAME of the env var holding the LLM credential (default: DEEPSEEK_API_KEY)")
    serve.add_argument("--llm-upstream", default=None, help="LLM egress target (default: https://api.deepseek.com)")
    serve.add_argument("--metadata-backend", default=None, choices=["memory", "postgres"],
                       help="metadata store backend (default: memory)")
    serve.add_argument("--postgres-dsn", default=None, help="postgresql://user:pass@host:port/db (with metadata_backend=postgres)")
    serve.add_argument("--kv-backend", default=None, choices=["memory", "redis"],
                       help="hot-state KV backend (default: memory)")
    serve.add_argument("--redis-url", default=None, help="redis://[:pass@]host:port/db (with kv_backend=redis)")
    serve.add_argument("--max-live-sessions", type=int, default=None,
                       help="cap on concurrent live sessions (default: uncapped)")
    serve.add_argument("--driver", default=None, choices=["process", "runsc", "microsandbox"],
                       help="sandbox substrate for this node (ADR-0012; default: process)")
    serve.add_argument("--snapshot-mode", default=None, choices=["full", "delta"],
                       help="snapshot encoding (ADR-0012; default: full)")
    serve.add_argument("--snapshot-chain-max", type=int, default=None,
                       help="delta chain compaction bound (default: 16)")
    serve.set_defaults(func=cmd_serve)

    config = sub.add_parser("config", help="configuration introspection (ADR-0009)")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    show = config_sub.add_parser("show", help="print the effective configuration as TOML")
    show.add_argument("--config", default=None,
                      help="path to whirlwind.toml (default: $WHIRLWIND_CONFIG or ./whirlwind.toml)")
    show.set_defaults(func=cmd_config_show)

    image = sub.add_parser("image")
    image_sub = image.add_subparsers(dest="image_command", required=True)
    build = image_sub.add_parser("build")
    build.add_argument("name", choices=["echo", "dsh"])
    build.set_defaults(func=cmd_image_build)

    agent = sub.add_parser("agent")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    create = agent_sub.add_parser("create")
    create.add_argument("name")
    create.add_argument("--harness", default=None, help="harness adapter id (required without --harness-bundle)")
    create.add_argument("--image", default=None, help="image ref (required without --harness-bundle)")
    create.add_argument("--harness-bundle", default=None,
                        help="named harness bundle (ADR-0011); supplies harness/image_ref when given")
    create.add_argument("--agent-version", default="1.0.0")
    create.add_argument("--model", action="append", help="model_config_decl entry key=value (repeatable)")
    create.add_argument("--seam", action="append", help="seam=provider binding (repeatable)")
    create.add_argument("--seam-instance", action="append",
                        help="named seam instance to bind (repeatable, ADR-0011)")
    create.add_argument("--skill", action="append", help="name=version skill ref (repeatable)")
    create.add_argument(
        "--env",
        action="append",
        help="secret env var: NAME (value read from local env) or NAME=VALUE (repeatable)",
    )
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
