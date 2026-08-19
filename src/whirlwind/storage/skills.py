"""Filesystem skill archive storage shared by MetadataStore implementations.

Skill archives are content blobs (the ObjectStore's concern in the cluster
form); metadata stores own only the versioned directory layout:
`<root>/<name>/<version>/`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from whirlwind.core import SkillRef


class SkillArchives:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, name: str, version: str, archive: Path) -> SkillRef:
        dest = self.root / name / version
        dest.mkdir(parents=True, exist_ok=True)
        if archive.is_dir():
            shutil.copytree(archive, dest, dirs_exist_ok=True)
        else:
            shutil.copy(archive, dest / archive.name)
        return SkillRef(name=name, version=version)

    def path(self, ref: SkillRef) -> Path | None:
        candidate = self.root / ref.name / ref.version
        return candidate if candidate.exists() else None
