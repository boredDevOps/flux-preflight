"""A small Flux repository in a temporary git, changed per test and checked like a merge request."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from flux_preflight import cli

# One cluster, laid out the common way: a root that applies Flux Kustomizations, one per part.
BASE: dict[str, str] = {
    ".flux-preflight.yaml": """\
mode: warn
schemas: []
limits:
  max_deletions: 10
""",
    "clusters/prod/flux-system/gotk-sync.yaml": """\
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: flux-system
  namespace: flux-system
spec:
  interval: 1m0s
  ref:
    branch: main
  url: https://git.example.invalid/flux.git
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: flux-system
  namespace: flux-system
spec:
  interval: 10m0s
  path: ./clusters/prod
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
""",
    "clusters/prod/flux-system/kustomization.yaml": """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - gotk-sync.yaml
""",
    "clusters/prod/kustomizations.yaml": """\
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: apps
  namespace: flux-system
spec:
  interval: 5m
  path: ./apps
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: data
  namespace: flux-system
spec:
  interval: 5m
  path: ./data
  prune: true
  dependsOn:
    - name: apps
  sourceRef:
    kind: GitRepository
    name: flux-system
""",
    "apps/kustomization.yaml": """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - web.yaml
  - config.yaml
""",
    "apps/namespace.yaml": """\
apiVersion: v1
kind: Namespace
metadata:
  name: shop
""",
    "apps/web.yaml": """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: shop
spec:
  replicas: 2
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
        - name: web
          image: nginx:1.27.0
""",
    "apps/config.yaml": """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: web
  namespace: shop
data:
  mode: production
""",
    "data/kustomization.yaml": """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - repository.yaml
  - redis.yaml
  - db.yaml
""",
    "data/repository.yaml": """\
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: bitnami
  namespace: flux-system
spec:
  interval: 1h
  url: https://charts.example.invalid
""",
    "data/redis.yaml": """\
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: redis
  namespace: shop
spec:
  interval: 10m
  chart:
    spec:
      chart: redis
      version: 19.6.4
      sourceRef:
        kind: HelmRepository
        name: bitnami
        namespace: flux-system
  values:
    architecture: standalone
""",
    "data/db.yaml": """\
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
  namespace: shop
spec:
  serviceName: db
  selector:
    matchLabels:
      app: db
  template:
    metadata:
      labels:
        app: db
    spec:
      containers:
        - name: db
          image: postgres:16.4
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 10Gi
""",
}


@dataclass
class Checked:
    code: int
    report: dict[str, Any]
    markdown: str
    text: str

    def rules(self, severity: str | None = None) -> list[str]:
        return sorted(
            f["rule"]
            for f in self.report["findings"]
            if (severity is None or f["severity"] == severity) and not f["allowed"]
        )

    def find(self, rule: str, subject: str = "") -> dict[str, Any]:
        for f in self.report["findings"]:
            if f["rule"] == rule and subject in f["subject"]:
                return f
        raise AssertionError(f"no {rule} on {subject!r}: {self.rules()}")

    @property
    def cluster(self) -> dict[str, Any]:
        return self.report["clusters"][0]


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")
        self.write(BASE)
        self.base = self.commit("base")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def write(self, files: dict[str, str | None]) -> None:
        for name, content in files.items():
            target = self.path / name
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def edit(self, name: str, old: str, new: str) -> None:
        target = self.path / name
        text = target.read_text(encoding="utf-8")
        assert old in text, f"{old!r} not in {name}"
        target.write_text(text.replace(old, new, 1), encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def check(
        self,
        capsys: pytest.CaptureFixture[str],
        *,
        mode: str | None = None,
        description: str = "",
        target: str = "main",
    ) -> Checked:
        self.commit("change")
        out = self.path.parent / "out"
        args = ["--repo", str(self.path), "--base", self.base, "--out", str(out)]
        args += ["--target-branch", target]
        if mode:
            args += ["--mode", mode]
        if description:
            desc = self.path.parent / "description.md"
            desc.write_text(description, encoding="utf-8")
            args += ["--description-file", str(desc)]
        code = cli.main(args)
        text = capsys.readouterr().out
        return Checked(
            code,
            json.loads((out / "report.json").read_text(encoding="utf-8")),
            (out / "report.md").read_text(encoding="utf-8"),
            text,
        )


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    if not shutil.which("flux") and not os.environ.get("FLUX_PREFLIGHT_NO_FLUX"):
        pytest.fail(
            "flux is not installed: run the tests in the image (docker build --target test)"
        )
    if not shutil.which("flux"):
        pytest.skip("flux is not installed")
    return Repo(tmp_path / "repo")
