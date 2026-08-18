"""Image registry interfaces (ADR D6).

An image is a self-contained runtime directory (bundle) the Hostlet can launch
inside a sandbox: venv + launcher + image-level default env (never secrets).
For M1 the bundle is a local directory managed by `LocalRegistry`; the
interface is shaped so a cluster deployment can swap in an OCI-backed
implementation (pull + unpack) without touching callers.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from argus.core.errors import ArgusError


class ImagingError(ArgusError):
    code = "argus/imaging"


class ImageNotFound(ImagingError):
    code = "argus/imaging/not-found"


@dataclass(slots=True)
class ImageBundle:
    """A resolved, launchable image (registry output, Hostlet input)."""

    ref: str
    root: Path
    launcher: list[str]  # absolute argv; argv[0] exists inside root
    env: dict[str, str]  # image-level defaults; secrets never live here
    harness: str  # selects the HarnessAdapter ("dsh" | "echo" | ...)


@dataclass(slots=True)
class ImageBuild:
    """Declarative build recipe. Placeholders, rendered at build time:

    {python}        absolute path of the bundle venv interpreter
    {root}          absolute bundle root (images/{name}/{version})
    {site_packages} absolute site-packages dir of the bundle venv
    """

    name: str
    version: str
    harness: str
    requirements: list[str] = field(default_factory=list)  # real pip specs
    launcher_argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)  # rel path -> content


def echo_image_build(repo_root: Path, version: str = "0.1.0") -> ImageBuild:
    """The echo conformance image: this repo installed into the bundle venv."""
    return ImageBuild(
        name="echo",
        version=version,
        harness="echo",
        requirements=[f"argus @ file://{repo_root}"],
        launcher_argv=["{python}", "-m", "argus.harness.echo_server"],
    )


# The dsh launcher resolves the platform-tagged single-file runtime exe shipped
# inside the image venv (linux/macos x x64/arm64 wheels) and execs it.
DSH_LAUNCHER_SRC = """import os
from deepseek_harness_runtime import resolve_bundled_launch_args
argv = resolve_bundled_launch_args()
os.execv(argv[0], list(argv))
"""


def _platform_tag() -> str:
    plat = {"linux": "linux", "darwin": "macos"}.get(sys.platform)
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine().lower()
    )
    if plat is None or arch is None:
        raise ImagingError(f"no dsh runtime exists for {sys.platform}/{platform.machine()}")
    return f"{plat}-{arch}"


def dsh_source_root(repo_root: Path) -> Path:
    """Locate the deepseek-harness checkout the dsh image installs from.

    Neither deepseek-harness-sdk nor deepseek-harness-runtime-bin is on PyPI:
    both come from a source checkout whose single-file runtime exe has been
    built. Resolution order: $ARGUS_DSH_REPO, then {repo_root}/refs/deepseek-harness.
    """
    root = Path(os.environ.get("ARGUS_DSH_REPO") or (repo_root / "refs" / "deepseek-harness"))
    for rel in ("python/sdk/pyproject.toml", "python/sdk-runtime/pyproject.toml"):
        if not (root / rel).is_file():
            raise ImagingError(
                f"dsh source checkout incomplete at {root} (missing {rel}); set ARGUS_DSH_REPO "
                "to a deepseek-harness checkout"
            )
    exe = root / "python/sdk-runtime/src/deepseek_harness_runtime/runtime" / (
        f"dsh-jsonrpc-agent-pkg-{_platform_tag()}"
    )
    if not exe.is_file():
        raise ImagingError(
            f"dsh runtime executable missing at {exe}; build it first: "
            "pnpm install && pnpm exec tsx scripts/build-exe-for-python-sdk.ts "
            "(in the deepseek-harness checkout)"
        )
    return root


def dsh_image_build(repo_root: Path, version: str = "0.1.0rc7") -> ImageBuild:
    """The native DeepSeek Harness image: SDK + bundled runtime exe + argus
    (the SandboxAgent rides inside every platform image)."""
    dsh_root = dsh_source_root(repo_root)
    return ImageBuild(
        name="dsh",
        version=version,
        harness="dsh",
        requirements=[
            f"deepseek-harness-runtime-bin @ file://{dsh_root}/python/sdk-runtime",
            f"deepseek-harness-sdk @ file://{dsh_root}/python/sdk",
            f"argus @ file://{repo_root}",
        ],
        launcher_argv=["{python}", "{root}/launch.py"],
        env={
            "DSH_CORDIS_CONFIG": "{site_packages}/deepseek_harness_runtime/runtime/cordis.yml",
        },
        files={"launch.py": DSH_LAUNCHER_SRC},
    )


@runtime_checkable
class ImageRegistry(Protocol):
    async def resolve(self, ref: str) -> ImageBundle: ...
    async def register(self, build: ImageBuild) -> str: ...
