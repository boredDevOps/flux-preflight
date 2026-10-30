"""The pieces on their own: garbage collection, YAML, schemas, versions, exit codes."""

from __future__ import annotations

import subprocess

import pytest

from flux_preflight.check import Result, allowances
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
    problems = _lint("on: yes\nmode: 0755\na: 1\na: 2\n")
    assert [(p.line, p.level, p.rule) for p in problems] == [
        (1, "warning", "truthy"),
        (2, "warning", "octal-values"),
        (4, "error", "key-duplicates"),
    ]
    assert problems[2] == Problem(4, "error", 'duplication of key "a" in mapping', "key-duplicates")
    assert _lint("a: 1\n") == []


def test_yamllint_syntax_error_reads_back() -> None:
    [problem] = _lint("a: [\n")
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
        _lint("a: 1\n")
