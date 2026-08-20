"""Capability seams: definitions, providers, renderer, injection manifest, catalog."""

from .model import (
    BUILTIN_PROVIDERS,
    BUILTIN_SEAMS,
    FS_V1,
    InjectionManifest,
    MEMORY_V1,
    ProviderSpec,
    SHELL_V1,
    SeamBinding,
    SeamConsumer,
    SeamDefinition,
    SeamRegistry,
    SeamRenderer,
    SeamTool,
    SkillInjection,
    WEB_V1,
)

__all__ = [
    "BUILTIN_PROVIDERS", "BUILTIN_SEAMS", "FS_V1", "MEMORY_V1", "SHELL_V1", "WEB_V1",
    "InjectionManifest", "ProviderSpec", "SeamBinding", "SeamConsumer", "SeamDefinition",
    "SeamRegistry", "SeamRenderer", "SeamTool", "SkillInjection",
]


def __getattr__(name: str):
    # catalog.py needs a MetadataStore import; keep it off the eager path so
    # sandbox-side importers of seam.model stay light (ADR-0011 D6).
    if name == "SeamCatalog":
        from .catalog import SeamCatalog

        globals()[name] = SeamCatalog
        return SeamCatalog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    return sorted(set(__all__) | {"SeamCatalog"})
