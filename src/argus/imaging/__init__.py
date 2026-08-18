"""Image registry: bundles a harness runtime into a launchable unit (ADR D6)."""

from .base import (
    DSH_LAUNCHER_SRC,
    ImageBuild,
    ImageBundle,
    ImageNotFound,
    ImagingError,
    dsh_image_build,
    echo_image_build,
)
from .local import LocalRegistry

__all__ = [
    "DSH_LAUNCHER_SRC", "ImageBuild", "ImageBundle", "ImageNotFound", "ImagingError",
    "LocalRegistry", "dsh_image_build", "echo_image_build",
]
