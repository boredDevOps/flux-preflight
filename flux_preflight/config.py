"""`.flux-preflight.yaml` in the checked repository. Every field is optional."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flux_preflight.yamlfiles import load_all

CONFIG_FILE = ".flux-preflight.yaml"

# Where kubeconform looks for schemas, after the CRDs the cluster's own Git defines.
DEFAULT_SCHEMAS = [
    "default",
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
]


@dataclass
class ClusterConfig:
    sync: str
    name: str | None = None


@dataclass
class Config:
    mode: str = "warn"
    clusters: list[ClusterConfig] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)
    max_deletions: int = 10
    max_deletion_ratio: float = 0.2
    kubernetes_version: str = "1.32.0"
    schemas: list[str] = field(default_factory=lambda: list(DEFAULT_SCHEMAS))
    schema_check: bool = True


class ConfigError(Exception):
    pass


def load_config(repo: Path) -> Config:
    path = repo / CONFIG_FILE
    if not path.is_file():
        return Config()
    docs = load_all(path.read_text(encoding="utf-8"))
    raw: dict[str, Any] = docs[0] if docs else {}
    config = Config()
    unknown = set(raw) - {
        "mode",
        "clusters",
        "ignore",
        "limits",
        "kubernetes_version",
        "schemas",
        "schema_check",
    }
    if unknown:
        raise ConfigError(f"{CONFIG_FILE}: unknown fields {sorted(unknown)}")
    if "mode" in raw:
        config.mode = str(raw["mode"])
    if config.mode not in ("warn", "block"):
        raise ConfigError(f"{CONFIG_FILE}: mode must be warn or block, not {config.mode!r}")
    for entry in raw.get("clusters") or []:
        if not isinstance(entry, dict) or not entry.get("sync"):
            raise ConfigError(f"{CONFIG_FILE}: each cluster needs `sync:`, the file of its root")
        config.clusters.append(ClusterConfig(sync=str(entry["sync"]), name=entry.get("name")))
    config.ignore = [str(item).strip("/") for item in raw.get("ignore") or []]
    limits = raw.get("limits") or {}
    config.max_deletions = int(limits.get("max_deletions", config.max_deletions))
    config.max_deletion_ratio = float(limits.get("max_deletion_ratio", config.max_deletion_ratio))
    if "kubernetes_version" in raw:
        config.kubernetes_version = str(raw["kubernetes_version"])
    if "schemas" in raw:
        config.schemas = [str(item) for item in raw["schemas"] or []]
    if "schema_check" in raw:
        config.schema_check = bool(raw["schema_check"])
    return config
