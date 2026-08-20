"""Typed catalog over the generic catalog-doc store (ADR-0011 D2/D6).

`SeamCatalog` wraps the four generic `MetadataStore` catalog methods with
SeamTemplate/SeamInstance pydantic models and the validation that belongs to
the domain:

- template registration validates against the SAME SeamRegistry the renderer
  uses (a template can never declare something unrenderable);
- instance resolution validates params against the template spec and
  materializes a legacy inline `SeamBindingDecl` via the substitution engine;
- `resolve_version_bindings` merges a version's instance references with its
  inline bindings, failing closed on seam collisions — the output feeds the
  unchanged, pure `SeamRenderer`.

Instance references are live (ConfigMap semantics): resolution happens at
provision time, so an updated instance affects sandboxes provisioned
afterwards; the persisted InjectionManifest snapshots the resolved values.
"""

from __future__ import annotations

from whirlwind.core import (
    AgentVersion,
    SeamBindingDecl,
    SeamError,
    SeamInstance,
    SeamTemplate,
)
from whirlwind.storage.providers import MetadataStore

from whirlwind.seam.model import (
    SeamRegistry,
    render_template,
    validate_template_params,
)

_KIND_TEMPLATE = "seam_template"
_KIND_INSTANCE = "seam_instance"


class SeamCatalog:
    """Templates + instances addressed by name, stored as catalog docs."""

    def __init__(self, store: MetadataStore, registry: SeamRegistry | None = None) -> None:
        self._store = store
        self._registry = registry or SeamRegistry()

    # ------------------------------------------------------------ templates

    async def put_template(self, template: SeamTemplate) -> SeamTemplate:
        """Create/replace a template (upsert). Validates seam/provider against
        the renderer's registry and body placeholders against declared params."""
        validate_template_params(template, self._registry)
        existing = await self.get_template(template.name)
        if existing is not None:
            template = template.model_copy(update={"created_at": existing.created_at})
        await self._store.put_catalog_doc(_KIND_TEMPLATE, template.model_dump(mode="json"))
        return template

    async def get_template(self, name: str) -> SeamTemplate | None:
        doc = await self._store.get_catalog_doc(_KIND_TEMPLATE, name)
        return SeamTemplate.model_validate(doc) if doc is not None else None

    async def list_templates(self) -> list[SeamTemplate]:
        return [
            SeamTemplate.model_validate(doc)
            for doc in await self._store.list_catalog_docs(_KIND_TEMPLATE)
        ]

    async def delete_template(self, name: str) -> None:
        await self._store.delete_catalog_doc(_KIND_TEMPLATE, name)

    # ------------------------------------------------------------ instances

    async def put_instance(self, instance: SeamInstance) -> SeamInstance:
        """Create/replace an instance. Validates the template exists and the
        params satisfy its spec, so a stored instance is always resolvable."""
        template = await self.get_template(instance.template)
        if template is None:
            raise SeamError(
                f"instance {instance.name!r} references unknown template {instance.template!r}"
            )
        # fail at write time on params the template rejects
        render_template(template, instance.params)
        existing = await self.get_instance(instance.name)
        if existing is not None:
            instance = instance.model_copy(update={"created_at": existing.created_at})
        await self._store.put_catalog_doc(_KIND_INSTANCE, instance.model_dump(mode="json"))
        return instance

    async def get_instance(self, name: str) -> SeamInstance | None:
        doc = await self._store.get_catalog_doc(_KIND_INSTANCE, name)
        return SeamInstance.model_validate(doc) if doc is not None else None

    async def list_instances(self) -> list[SeamInstance]:
        return [
            SeamInstance.model_validate(doc)
            for doc in await self._store.list_catalog_docs(_KIND_INSTANCE)
        ]

    async def delete_instance(self, name: str) -> None:
        await self._store.delete_catalog_doc(_KIND_INSTANCE, name)

    # ------------------------------------------------------------ resolution

    async def resolve_instance(self, instance: SeamInstance) -> SeamBindingDecl:
        """Materialize one instance through its template (provenance stamped)."""
        template = await self.get_template(instance.template)
        if template is None:
            raise SeamError(
                f"instance {instance.name!r} references unknown template {instance.template!r}"
            )
        decl = render_template(template, instance.params)
        return decl.model_copy(update={"instance": instance.name})

    async def resolve_version_bindings(self, version: AgentVersion) -> list[SeamBindingDecl]:
        """Instance refs + inline bindings, dedup-checked by seam id (D2/D4)."""
        resolved: list[SeamBindingDecl] = []
        seen: set[str] = set()
        for decl in version.seam_bindings:
            if decl.seam in seen:
                raise SeamError(f"duplicate seam binding {decl.seam!r}")
            seen.add(decl.seam)
            resolved.append(decl)
        for name in version.seam_instances:
            instance = await self.get_instance(name)
            if instance is None:
                raise SeamError(
                    f"version {version.id} references unknown seam instance {name!r}"
                )
            decl = await self.resolve_instance(instance)
            if decl.seam in seen:
                raise SeamError(
                    f"seam instance {name!r} collides with an existing binding for "
                    f"seam {decl.seam!r}"
                )
            seen.add(decl.seam)
            resolved.append(decl)
        return resolved
