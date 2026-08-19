"""LocalRegistry: directory-backed image registry with a real pip builder.

Layout:
    {root}/images/{name}/{version}/
        venv/            # real virtualenv, deps really installed at build time
        launch.py ...    # extra files from the build recipe
        manifest.json    # rendered launcher/env + provenance

Build = `python -m venv` + `pip install <requirements>` + placeholder render.
Nothing is cached or faked: a resolved bundle's venv is a working interpreter
with the declared packages installed.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

from whirlwind.core.errors import Conflict

from .base import ImageBuild, ImageBundle, ImageNotFound, ImagingError

_PLACEHOLDER = re.compile(r"\{(python|root|site_packages)\}")


def _version_key(version: str) -> list[int]:
    parts: list[int] = []
    for chunk in version.split("."):
        digits = re.match(r"\d+", chunk)
        parts.append(int(digits.group()) if digits else 0)
    return parts


class LocalRegistry:
    def __init__(self, root: Path) -> None:
        self._root = root

    async def resolve(self, ref: str) -> ImageBundle:
        name, _, version = ref.partition("@")
        version_dir = self._find_version_dir(name, version or None)
        if version_dir is None:
            raise ImageNotFound(f"no image matches ref {ref!r} under {self._root}")
        manifest_path = version_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ImageNotFound(f"image manifest unreadable at {manifest_path}: {exc}") from exc
        launcher = list(manifest["launcher"])
        if not Path(launcher[0]).is_file():
            raise ImageNotFound(f"image {ref} launcher missing: {launcher[0]}")
        return ImageBundle(
            ref=f"{manifest['name']}@{manifest['version']}",
            root=version_dir,
            launcher=launcher,
            env=dict(manifest.get("env", {})),
            harness=manifest["harness"],
        )

    async def register(self, build: ImageBuild) -> str:
        version_dir = self._root / "images" / build.name / build.version
        if (version_dir / "manifest.json").exists():
            raise Conflict(f"image {build.name}@{build.version} already registered")
        version_dir.mkdir(parents=True, exist_ok=True)
        venv_dir = version_dir / "venv"
        try:
            await self._run([sys.executable, "-m", "venv", str(venv_dir)], version_dir)
            if build.requirements:
                await self._pip_install(venv_dir, build.requirements)
            site_packages = await self._site_packages(venv_dir)
            rendered = {
                "python": str(venv_dir / "bin" / "python"),
                "root": str(version_dir),
                "site_packages": site_packages,
            }
            for rel, content in build.files.items():
                target = version_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            launcher = [_PLACEHOLDER.sub(lambda m: rendered[m.group(1)], part) for part in build.launcher_argv]
            if not launcher:
                raise ImagingError("build recipe has no launcher_argv")
            env = {k: _PLACEHOLDER.sub(lambda m: rendered[m.group(1)], v) for k, v in build.env.items()}
            manifest = {
                "name": build.name,
                "version": build.version,
                "harness": build.harness,
                "launcher": launcher,
                "env": env,
                "requirements": build.requirements,
                "created_at": int(time.time() * 1000),
            }
            (version_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        except Exception:
            # a failed build leaves no half image behind
            import shutil

            shutil.rmtree(version_dir, ignore_errors=True)
            raise
        return f"{build.name}@{build.version}"

    # ------------------------------------------------------------ internals

    def _find_version_dir(self, name: str, version: str | None) -> Path | None:
        images = self._root / "images" / name
        if not images.is_dir():
            return None
        if version is not None:
            candidate = images / version
            return candidate if (candidate / "manifest.json").is_file() else None
        versions = [d for d in images.iterdir() if (d / "manifest.json").is_file()]
        if not versions:
            return None
        return max(versions, key=lambda d: _version_key(d.name))

    @staticmethod
    async def _run(argv: list[str], cwd: Path, timeout_s: float = 600.0) -> str:
        # Building is host-side tooling: full host env (pip index, proxy, PATH).
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ImagingError(f"build step timed out: {argv}") from None
        if proc.returncode != 0:
            raise ImagingError(
                f"build step failed ({argv[0]} rc={proc.returncode}): {out.decode(errors='replace')[-2000:]}"
            )
        return out.decode(errors="replace")

    async def _pip_install(self, venv_dir: Path, requirements: list[str]) -> None:
        await self._run(
            [str(venv_dir / "bin" / "python"), "-m", "pip", "install", "--disable-pip-version-check", "-q"]
            + list(requirements),
            venv_dir,
        )

    async def _site_packages(self, venv_dir: Path) -> str:
        out = await self._run(
            [
                str(venv_dir / "bin" / "python"),
                "-c",
                "import sysconfig; print(sysconfig.get_paths()['purelib'])",
            ],
            venv_dir,
        )
        return out.strip()
