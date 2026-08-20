"""Harness bundles: the integral harness image combination, first-class (ADR-0011 D5).

A `HarnessBundle` names what it takes to run a harness — adapter id, built
image, optional entrypoint/env overlay, and a declarative `native_seams`
surface. Bundles are catalog docs (kind `harness_bundle`); the builtins `echo`
and `dsh` resolve as fallbacks when not present in the store, so the concept
is bindable from day one with zero seeding ceremony.

`native_seams` is a declarative mirror of what the adapter actually mounts
natively; the adapter remains the executor of truth and still fails closed on
a seam it cannot mount (capability-honesty rule).
"""

from __future__ import annotations

from whirlwind.core import HarnessBundle, HarnessError
from whirlwind.storage.providers import MetadataStore

_KIND = "harness_bundle"

BUILTIN_HARNESS_BUNDLES: dict[str, HarnessBundle] = {
    "echo": HarnessBundle(
        name="echo",
        harness="echo",
        image_ref="echo",
        version="0.1.0",
        description="Echo conformance harness: this repo installed into the bundle venv",
        native_seams=[],
    ),
    "dsh": HarnessBundle(
        name="dsh",
        harness="dsh",
        image_ref="dsh",
        version="0.1.0rc7",
        description=(
            "DeepSeek Harness as an integral image combination: "
            "dsh SDK + bundled runtime exe + whirlwind SandboxAgent"
        ),
        native_seams=["fs.v1", "shell.v1", "memory.v1"],
    ),
}


class HarnessBundles:
    """CRUD + resolution over harness-bundle catalog docs, with builtin fallbacks."""

    def __init__(self, store: MetadataStore) -> None:
        self._store = store

    async def put(self, bundle: HarnessBundle) -> HarnessBundle:
        existing = await self.get(bundle.name)
        if existing is not None:
            bundle = bundle.model_copy(update={"created_at": existing.created_at})
        await self._store.put_catalog_doc(_KIND, bundle.model_dump(mode="json"))
        return bundle

    async def get(self, name: str) -> HarnessBundle | None:
        doc = await self._store.get_catalog_doc(_KIND, name)
        if doc is not None:
            return HarnessBundle.model_validate(doc)
        return BUILTIN_HARNESS_BUNDLES.get(name)

    async def list(self) -> list[HarnessBundle]:
        stored = [
            HarnessBundle.model_validate(doc)
            for doc in await self._store.list_catalog_docs(_KIND)
        ]
        by_name = {b.name: b for b in stored}
        # builtins fill the gaps; a stored doc with the same name shadows them
        for name, builtin in BUILTIN_HARNESS_BUNDLES.items():
            by_name.setdefault(name, builtin)
        return sorted(by_name.values(), key=lambda b: b.name)

    async def delete(self, name: str) -> None:
        await self._store.delete_catalog_doc(_KIND, name)

    async def resolve(self, version_harness_bundle: str) -> HarnessBundle:
        """Resolve a version's bundle reference, fail-closed on unknowns."""
        bundle = await self.get(version_harness_bundle)
        if bundle is None:
            raise HarnessError(f"unknown harness bundle {version_harness_bundle!r}")
        return bundle
