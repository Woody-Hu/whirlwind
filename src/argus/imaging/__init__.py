"""Image registry: bundles a harness runtime into a launchable unit (ADR D6)."""

from .base import (
    DSH_LAUNCHER_SRC,
    ImageBuild,
    ImageBundle,
    ImageNotFound,
    ImageRegistry,
    ImagingError,
    dsh_image_build,
    dsh_source_root,
    echo_image_build,
)
from .local import LocalRegistry

__all__ = [
    "DSH_LAUNCHER_SRC", "ImageBuild", "ImageBundle", "ImageNotFound", "ImageRegistry",
    "ImagingError", "LocalRegistry", "dsh_image_build", "dsh_source_root", "echo_image_build",
]
