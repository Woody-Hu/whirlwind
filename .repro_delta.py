"""Repro: which files differ between pre-suspend workspace and post-restore workspace?"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/tests")

from whirlwind.imaging import LocalRegistry, echo_image_build

REPO_ROOT = Path("/workspace")


async def main() -> None:
    from tests.integration.test_delta_suspend import _make_hostlet
    from whirlwind.drivers.process import _scan_tree

    tmp = Path("/tmp/repro-delta")
    if tmp.exists():
        import shutil

        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    registry = LocalRegistry(tmp / "images")
    await registry.register(echo_image_build(REPO_ROOT))

    hostlet, store, session, version = await _make_hostlet(tmp, registry)
    try:
        sandbox = await hostlet.ensure(session, version)
        ws = Path(sandbox.workspace)
        (ws / "seed.txt").write_text("S" * 1024)
        (ws / "log.jsonl").write_text('{"cycle":0}\n')

        before = _scan_tree(ws)
        snap = await hostlet.suspend(sandbox.id)
        sandbox2 = await hostlet.restore(session, version)
        ws2 = Path(sandbox2.workspace)
        after = _scan_tree(ws2)

        print("same workspace path:", ws == ws2)
        for rel in sorted(set(before[0]) | set(after[0])):
            b, a = before[0].get(rel), after[0].get(rel)
            if b != a:
                print(f"DIFF {rel}: {b} -> {a}")
        for d in sorted(before[1] ^ after[1]):
            print(f"DIR-DIFF {d}")
        await hostlet.destroy(sandbox2.id)
    finally:
        await hostlet.aclose()


asyncio.run(main())
