"""`.flux-preflight.yaml` in the checked repository. Every field is optional."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flux_preflight.yamlfiles import YamlProblem, iter_docs

CONFIG_FILE = ".flux-preflight.yaml"

# Where kubeconform looks for schemas, after the CRDs the cluster's own Git defines.
DEFAULT_SCHEMAS = [
    "default",
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
]

_FIELDS = {
    "mode",
    "clusters",
    "ignore",
    "limits",
    "timeouts",
    "kubernetes_version",
    "schemas",
    "schema_check",
}
_CLUSTER_FIELDS = {"sync", "name"}
_LIMIT_FIELDS = {
    "max_deletions",
    "max_deletion_ratio",
    "min_objects_for_ratio",
    "min_secret_length",
}
_TIMEOUT_FIELDS = {"flux_build_seconds", "kubeconform_seconds", "yamllint_seconds"}
_KUBERNETES_VERSION = re.compile(r"\d+\.\d+\.\d+|master")


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
    min_objects_for_ratio: int = 5
    min_secret_length: int = 6
    flux_build_seconds: int = 180
    kubeconform_seconds: int = 600
    yamllint_seconds: int = 60
    kubernetes_version: str = "1.32.0"
    schemas: list[str] = field(default_factory=lambda: list(DEFAULT_SCHEMAS))
    schema_check: bool = True


class ConfigError(Exception):
    pass


def _problem(message: str) -> ConfigError:
    return ConfigError(f"{CONFIG_FILE}: {message}")


def _known(section: dict[Any, Any], allowed: set[str], where: str = "") -> None:
    unknown = {str(key) for key in section} - allowed
    if unknown:
        raise _problem(f"unknown fields {sorted(unknown)}" + (f" in {where}" if where else ""))


def _section(raw: dict[str, Any], key: str, allowed: set[str]) -> dict[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _problem(f"{key} must be a mapping, not {value!r}")
    _known(value, allowed, key)
    return value


def _whole(section: dict[str, Any], where: str, key: str, default: int, least: int) -> int:
    value = section.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < least:
        raise _problem(f"{where}.{key} must be a whole number of at least {least}, not {value!r}")
    return int(value)


def _ratio(section: dict[str, Any], where: str, key: str, default: float) -> float:
    value = section.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise _problem(f"{where}.{key} must be a number from 0 to 1, not {value!r}")
    return float(value)


def _strings(raw: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _problem(f"{key} must be a list of strings, not {value!r}")
    return value


def _clusters(raw: dict[str, Any]) -> list[ClusterConfig]:
    value = raw.get("clusters")
    if value is None:
        return []
    if not isinstance(value, list):
        raise _problem(f"clusters must be a list, not {value!r}")
    clusters = []
    for entry in value:
        if not isinstance(entry, dict) or not isinstance(entry.get("sync"), str):
            raise _problem("each cluster needs `sync:`, the file of its root")
        _known(entry, _CLUSTER_FIELDS, "clusters")
        name = entry.get("name")
        if name is not None and not isinstance(name, str):
            raise _problem(f"clusters.name must be a string, not {name!r}")
        clusters.append(ClusterConfig(sync=entry["sync"], name=name))
    return clusters


def load_config(repo: Path) -> Config:
    path = repo / CONFIG_FILE
    if not path.is_file():
        return Config()
    try:
        docs = [doc for _, doc in iter_docs(path.read_text(encoding="utf-8")) if doc is not None]
    except YamlProblem as exc:
        raise _problem(f"does not parse: {exc}") from exc
    if len(docs) > 1 or (docs and not isinstance(docs[0], dict)):
        raise _problem("must be one YAML mapping")
    found: dict[str, Any] = docs[0] if docs else {}
    _known(found, _FIELDS)
    raw = {key: value for key, value in found.items() if value is not None}  # empty means unset
    config = Config()

    mode = raw.get("mode", config.mode)
    if mode not in ("warn", "block"):
        raise _problem(f"mode must be warn or block, not {mode!r}")
    config.mode = mode

    version = raw.get("kubernetes_version", config.kubernetes_version)
    if not isinstance(version, str) or not _KUBERNETES_VERSION.fullmatch(version):
        # An unquoted 1.30 is the number 1.3 by the time it gets here.
        raise _problem(f'kubernetes_version must be quoted, like "1.32.0", not {version!r}')
    config.kubernetes_version = version

    schema_check = raw.get("schema_check", config.schema_check)
    if not isinstance(schema_check, bool):
        raise _problem(f"schema_check must be true or false, not {schema_check!r}")
    config.schema_check = schema_check

    config.clusters = _clusters(raw)
    config.ignore = [item.strip("/") for item in _strings(raw, "ignore", [])]
    config.schemas = _strings(raw, "schemas", config.schemas)

    limits = _section(raw, "limits", _LIMIT_FIELDS)
    config.max_deletions = _whole(limits, "limits", "max_deletions", config.max_deletions, 0)
    config.max_deletion_ratio = _ratio(
        limits, "limits", "max_deletion_ratio", config.max_deletion_ratio
    )
    config.min_objects_for_ratio = _whole(
        limits, "limits", "min_objects_for_ratio", config.min_objects_for_ratio, 0
    )
    config.min_secret_length = _whole(
        limits, "limits", "min_secret_length", config.min_secret_length, 0
    )

    timeouts = _section(raw, "timeouts", _TIMEOUT_FIELDS)
    config.flux_build_seconds = _whole(
        timeouts, "timeouts", "flux_build_seconds", config.flux_build_seconds, 1
    )
    config.kubeconform_seconds = _whole(
        timeouts, "timeouts", "kubeconform_seconds", config.kubeconform_seconds, 1
    )
    config.yamllint_seconds = _whole(
        timeouts, "timeouts", "yamllint_seconds", config.yamllint_seconds, 1
    )
    return config
