from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import Role


_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RepositoryConfig:
    slug: str
    path: Path
    enabled: bool
    roles: tuple[Role, ...]


@dataclass(frozen=True)
class Registry:
    repositories: tuple[RepositoryConfig, ...]

    @property
    def enabled(self) -> list[RepositoryConfig]:
        return [repo for repo in self.repositories if repo.enabled]


def load_registry(path: Path) -> Registry:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load repository registry {path}: {exc}") from exc
    rows = payload.get("repositories")
    if not isinstance(rows, list):
        raise ConfigError("registry must contain a repositories list")

    repositories: list[RepositoryConfig] = []
    slugs: set[str] = set()
    paths: set[Path] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ConfigError("each repository entry must be an object")
        slug = row.get("slug")
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise ConfigError(f"invalid repository slug: {slug!r}")
        resolved = Path(str(row.get("path", ""))).expanduser().resolve()
        if not resolved.is_dir():
            raise ConfigError(f"repository path is not a directory: {resolved}")
        slug_key = slug.casefold()
        if slug_key in slugs:
            raise ConfigError(f"duplicate repository slug: {slug}")
        if resolved in paths:
            raise ConfigError(f"duplicate repository path: {resolved}")
        enabled = row.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigError(f"enabled must be a boolean for {slug}")
        try:
            roles = tuple(Role(value) for value in row.get("roles", ["pm", "dev", "qa"]))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid roles for {slug}") from exc
        if not roles or len(set(roles)) != len(roles):
            raise ConfigError(f"roles for {slug} must be unique and non-empty")
        repositories.append(
            RepositoryConfig(slug, resolved, enabled, roles)
        )
        slugs.add(slug_key)
        paths.add(resolved)
    return Registry(tuple(repositories))
