"""The pieces on their own: garbage collection, YAML, schemas, versions, exit codes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from flux_preflight.check import Result, allowances
from flux_preflight.config import CONFIG_FILE, ClusterConfig, Config, ConfigError, load_config
from flux_preflight.inventory import compare
from flux_preflight.model import (
    FLUX_KUSTOMIZE,
    PRUNE_ANNOTATION,
    Doc,
    Finding,
    Ref,
    Render,
    Severity,
)
from flux_preflight.render import BuildError, build
from flux_preflight.report import exit_code, markdown
from flux_preflight.rules.change import quantity
from flux_preflight.rules.hygiene import _plain_secret, unpinned
from flux_preflight.rules.lint import LintError, Problem, _lint
from flux_preflight.rules.schema import strict
from flux_preflight.yamlfiles import YamlProblem, load_all


def ks(name: str, prune: bool = True, disabled: bool = False) -> tuple[Ref, Doc]:
    meta: Doc = {"name": name, "namespace": "flux-system"}
    if disabled:
        meta["annotations"] = {PRUNE_ANNOTATION: "disabled"}
    doc = {"apiVersion": f"{FLUX_KUSTOMIZE}/v1", "kind": "Kustomization", "metadata": meta}
    doc["spec"] = {"prune": prune, "path": f"./{name}"}
    return Ref.of(doc), doc


def obj(kind: str, name: str, namespace: str = "shop", disabled: bool = False) -> tuple[Ref, Doc]:
    meta: Doc = {"name": name, "namespace": namespace}
    if disabled:
        meta["annotations"] = {PRUNE_ANNOTATION: "disabled"}
    doc = {"apiVersion": "v1", "kind": kind, "metadata": meta}
    return Ref.of(doc), doc


def render(tree: dict[str, list[tuple[Ref, Doc]]], kss: dict[str, tuple[Ref, Doc]]) -> Render:
    root_ref, _ = kss["root"]
    r = Render(cluster="c", root=root_ref)
    for name, items in tree.items():
        ref, doc = kss[name]
        r.kustomizations[ref] = doc
        r.inventories[ref] = dict(items)
        for child_ref, _ in items:
            if child_ref.is_flux_kustomization:
                r.parent[child_ref] = ref
    r.parent[root_ref] = None
    return r


def test_a_deleted_kustomization_takes_its_objects_and_its_children() -> None:
    kss = {"root": ks("root"), "apps": ks("apps"), "inner": ks("inner")}
    ns, web = obj("Namespace", "shop", ""), obj("ConfigMap", "web")
    base = render(
        {
            "root": [kss["root"], kss["apps"]],
            "apps": [ns, kss["inner"]],
            "inner": [web],
        },
        kss,
    )
    head = render({"root": [kss["root"]]}, kss)
    outcome = compare(base, head)
    assert {d.ref for d in outcome.deletions} == {kss["apps"][0], ns[0], kss["inner"][0], web[0]}
    assert {d.ref for d in outcome.taken_by(kss["apps"][0])} == {ns[0], kss["inner"][0], web[0]}


def test_prune_disabled_and_prune_false_keep_objects() -> None:
    kss = {"root": ks("root"), "apps": ks("apps", prune=False), "data": ks("data", disabled=True)}
    keep = obj("ConfigMap", "keep", disabled=True)
    plain = obj("ConfigMap", "plain")
    base = render(
        {
            "root": [kss["root"], kss["apps"], kss["data"]],
            "apps": [plain],
            "data": [keep, obj("ConfigMap", "other")],
        },
        kss,
    )
    head = render({"root": [kss["root"], kss["apps"]], "apps": []}, kss)
    outcome = compare(base, head)
    assert outcome.deletions == []
    assert {k.ref for k in outcome.kept} == {plain[0], kss["data"][0]}


@pytest.mark.parametrize(
    ("prune", "policy", "goes"),
    [
        (True, None, True),
        (False, None, False),
        (True, "MirrorPrune", True),
        (False, "MirrorPrune", False),
        (False, "Delete", True),
        (False, "WaitForTermination", True),
        (True, "Orphan", False),
    ],
)
def test_a_deleted_kustomization_follows_its_deletion_policy(
    prune: bool, policy: str | None, goes: bool
) -> None:
    kss = {"root": ks("root"), "apps": ks("apps", prune=prune)}
    if policy:
        kss["apps"][1]["spec"]["deletionPolicy"] = policy
    web = obj("ConfigMap", "web")
    base = render({"root": [kss["root"], kss["apps"]], "apps": [web]}, kss)
    head = render({"root": [kss["root"]]}, kss)
    outcome = compare(base, head)
    assert (web[0] in outcome.deleted) is goes
    assert (web[0] in {k.ref for k in outcome.kept}) is not goes


def test_an_object_another_kustomization_builds_moves() -> None:
    kss = {"root": ks("root"), "a": ks("a"), "b": ks("b")}
    ns = obj("Namespace", "shop", "")
    base = render({"root": [kss["root"], kss["a"], kss["b"]], "a": [ns], "b": []}, kss)
    head = render({"root": [kss["root"], kss["a"], kss["b"]], "a": [], "b": [ns]}, kss)
    outcome = compare(base, head)
    assert outcome.deletions == []
    assert [(m.old_owner, m.new_owner) for m in outcome.moves] == [(kss["a"][0], kss["b"][0])]


def test_a_kustomization_that_fails_to_build_deletes_nothing() -> None:
    kss = {"root": ks("root"), "apps": ks("apps")}
    base = render({"root": [kss["root"], kss["apps"]], "apps": [obj("ConfigMap", "x")]}, kss)
    head = render({"root": [kss["root"], kss["apps"]]}, kss)
    head.kustomizations[kss["apps"][0]] = kss["apps"][1]
    head.errors[kss["apps"][0]] = "broken"
    outcome = compare(base, head)
    assert outcome.deletions == [] and outcome.unknown == [kss["apps"][0]]


def test_a_root_that_stops_building_itself_deletes_everything() -> None:
    kss = {"root": ks("root"), "apps": ks("apps")}
    cm = obj("ConfigMap", "x")
    base = render({"root": [kss["root"], kss["apps"]], "apps": [cm]}, kss)
    head = render({"root": [kss["apps"]], "apps": [cm]}, kss)
    outcome = compare(base, head)
    assert kss["root"][0] in outcome.deleted
    # apps is still built by the root's last run, but the root's deletion takes it along.
    assert kss["apps"][0] in outcome.deleted or kss["apps"][0] in {m.ref for m in outcome.moves}


def test_duplicate_keys_are_errors_not_last_one_wins() -> None:
    with pytest.raises(YamlProblem, match="duplicate key 'a'"):
        load_all("a: 1\nb: 2\na: 3\n")
    assert load_all("x: &x {a: 1}\ny:\n  <<: *x\n  b: 2\n")[0]["y"] == {"a": 1, "b": 2}


def test_strict_schemas_refuse_unknown_fields_but_keep_open_ones_open() -> None:
    converted = strict(
        {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "properties": {"a": {"type": "string", "nullable": True}},
                },
                "values": {"type": "object", "x-kubernetes-preserve-unknown-fields": True},
            },
        }
    )
    assert converted["additionalProperties"] is False
    assert converted["properties"]["spec"]["additionalProperties"] is False
    assert converted["properties"]["spec"]["properties"]["a"]["type"] == ["string", "null"]
    assert "additionalProperties" not in converted["properties"]["values"]


@pytest.mark.parametrize(
    ("image", "floating"),
    [
        ("nginx", True),
        ("nginx:latest", True),
        ("registry:5000/team/app", True),
        ("registry:5000/team/app:1.2", False),
        ("nginx@sha256:abc", False),
    ],
)
def test_unpinned_images(image: str, floating: bool) -> None:
    assert unpinned(image) is floating


def test_quantities() -> None:
    assert quantity("10Gi") == 10 * 2**30
    assert quantity("500M") == 500 * 10**6
    assert quantity("nonsense") is None


def test_sops_and_placeholders_are_not_plain_secrets() -> None:
    secret = {"apiVersion": "v1", "kind": "Secret", "stringData": {"a": "real"}}
    assert _plain_secret(secret) == ["a"]
    assert _plain_secret({**secret, "sops": {"version": "3"}}) is None
    assert _plain_secret({**secret, "stringData": {"a": "${PASSWORD}"}}) is None


def test_allow_lines_name_exactly_one_finding() -> None:
    text = (
        "Why.\n\nflux-preflight allow GC101 prod:Namespace/shop\nnot flux-preflight allow GC1 x\n"
    )
    assert allowances(text) == {"GC101 prod:Namespace/shop"}


def test_exit_codes_and_the_note() -> None:
    blocking = Finding("GC101", Severity.BLOCK, "prod", "Namespace/shop", "goes")
    result = Result(findings=[blocking])
    assert exit_code(result, "warn") == 3 and exit_code(result, "block") == 1
    blocking.allowed = True
    assert exit_code(result, "block") == 0
    assert "Accepted in the description" in markdown(result, "block")
    tricky = Finding("GC102", Severity.WARN, "p", "ConfigMap/a`b", "x")
    assert "a'b" in markdown(Result(findings=[tricky]), "warn")


def test_yamllint_output_reads_back_as_problems() -> None:
    problems = _lint("on: yes\nmode: 0755\na: 1\na: 2\n", 60)
    assert [(p.line, p.level, p.rule) for p in problems] == [
        (1, "warning", "truthy"),
        (2, "warning", "octal-values"),
        (4, "error", "key-duplicates"),
    ]
    assert problems[2] == Problem(4, "error", 'duplication of key "a" in mapping', "key-duplicates")
    assert _lint("a: 1\n", 60) == []


def test_yamllint_syntax_error_reads_back() -> None:
    [problem] = _lint("a: [\n", 60)
    assert problem.level == "error" and problem.desc.startswith("syntax error:")


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(255, ""), (1, "something this version started to print\n")],
)
def test_yamllint_failing_is_not_read_as_clean(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    done = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="broken")
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: done)
    with pytest.raises(LintError):
        _lint("a: 1\n", 60)


def test_a_timeout_names_the_setting_that_raises_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def too_slow(*_: Any, **__: Any) -> None:
        raise subprocess.TimeoutExpired("program", 5)

    monkeypatch.setattr(subprocess, "run", too_slow)
    with pytest.raises(LintError, match=r"5 s \(timeouts\.yamllint_seconds\)"):
        _lint("a: 1\n", 5)
    ks_doc: Doc = {"metadata": {"name": "a"}, "spec": {"path": "./"}}
    with pytest.raises(BuildError, match=r"5 s \(timeouts\.flux_build_seconds\)"):
        build("flux", tmp_path, ks_doc, 5)


def _config(tmp_path: Path, text: str) -> Config:
    (tmp_path / CONFIG_FILE).write_text(text, encoding="utf-8")
    return load_config(tmp_path)


def test_no_file_an_empty_file_and_empty_fields_keep_the_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path) == Config()
    assert _config(tmp_path, "") == Config()
    empty = "mode:\nlimits:\ntimeouts:\nschemas:\nignore:\nclusters:\n"
    assert _config(tmp_path, empty) == Config()
    assert _config(tmp_path, "limits:\n  max_deletions:\n") == Config()


def test_every_explicit_value_replaces_its_default(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """\
mode: block
ignore: [no-run/]
clusters:
  - sync: clusters/a/gotk-sync.yaml
    name: a
limits:
  max_deletions: 0
  max_deletion_ratio: 0
  min_objects_for_ratio: 0
  min_secret_length: 0
timeouts:
  flux_build_seconds: 1
  kubeconform_seconds: 2
  yamllint_seconds: 3
kubernetes_version: "1.30.4"
schemas: []
schema_check: false
""",
    )
    assert config == Config(
        mode="block",
        clusters=[ClusterConfig("clusters/a/gotk-sync.yaml", "a")],
        ignore=["no-run"],
        max_deletions=0,
        max_deletion_ratio=0.0,
        min_objects_for_ratio=0,
        min_secret_length=0,
        flux_build_seconds=1,
        kubeconform_seconds=2,
        yamllint_seconds=3,
        kubernetes_version="1.30.4",
        schemas=[],
        schema_check=False,
    )


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("modee: warn\n", "unknown fields ['modee']"),
        ("limits:\n  max_deletion: 3\n", "unknown fields ['max_deletion'] in limits"),
        ("timeouts:\n  flux_seconds: 3\n", "unknown fields ['flux_seconds'] in timeouts"),
        ("clusters:\n  - sync: a\n    nam: b\n", "unknown fields ['nam'] in clusters"),
        ("limits: 5\n", "limits must be a mapping"),
        ("timeouts: 5\n", "timeouts must be a mapping"),
        ("limits:\n  max_deletions: abc\n", "limits.max_deletions must be a whole number"),
        ("limits:\n  max_deletions: true\n", "limits.max_deletions must be a whole number"),
        ("limits:\n  max_deletions: 1.5\n", "limits.max_deletions must be a whole number"),
        ("limits:\n  max_deletions: -1\n", "limits.max_deletions must be a whole number"),
        ("limits:\n  max_deletion_ratio: 1.5\n", "limits.max_deletion_ratio must be a number"),
        ("limits:\n  max_deletion_ratio: abc\n", "limits.max_deletion_ratio must be a number"),
        ("limits:\n  min_objects_for_ratio: -1\n", "limits.min_objects_for_ratio must be a"),
        ("limits:\n  min_secret_length: x\n", "limits.min_secret_length must be a"),
        ("timeouts:\n  flux_build_seconds: 0\n", "timeouts.flux_build_seconds must be a"),
        ("timeouts:\n  kubeconform_seconds: 0\n", "timeouts.kubeconform_seconds must be a"),
        ("timeouts:\n  yamllint_seconds: 0\n", "timeouts.yamllint_seconds must be a"),
        ("kubernetes_version: 1.30\n", "kubernetes_version must be quoted"),
        ('kubernetes_version: "v1.30.0"\n', "kubernetes_version must be quoted"),
        ("ignore: no-run\n", "ignore must be a list"),
        ("schemas: default\n", "schemas must be a list"),
        ("schema_check: 'false'\n", "schema_check must be true or false"),
        ("clusters: 5\n", "clusters must be a list"),
        ("clusters: [a]\n", "each cluster needs `sync:`"),
        ("clusters:\n  - sync: a\n    name: 5\n", "clusters.name must be a string"),
        ("mode: strict\n", "mode must be warn or block"),
        ("a: [\n", "does not parse"),
        ("- mode: block\n", "must be one YAML mapping"),
        ("mode: warn\n---\nmode: block\n", "must be one YAML mapping"),
    ],
)
def test_a_config_that_says_something_else_stops_the_check(
    tmp_path: Path, text: str, said: str
) -> None:
    with pytest.raises(ConfigError) as caught:
        _config(tmp_path, text)
    assert said in str(caught.value)
    assert str(caught.value).startswith(f"{CONFIG_FILE}: ")
