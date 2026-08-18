"""Capability seams: definitions, providers, renderer, injection manifest."""

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
