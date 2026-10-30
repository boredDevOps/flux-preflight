"""Does the cluster still build, and does everything it names exist."""

from __future__ import annotations

from flux_preflight.context import ClusterCheck
from flux_preflight.model import FLUX_HELM, FLUX_KUSTOMIZE, FLUX_SOURCE, Finding, Ref, Severity, dig
from flux_preflight.yamlfiles import KUSTOMIZE_CONFIG, referenced_non_resources


def check(cc: ClusterCheck) -> list[Finding]:
    return [
        *_builds(cc),
        *_dependencies(cc),
        *_owners(cc),
        *_sources(cc),
        *_orphans(cc),
    ]


def _builds(cc: ClusterCheck) -> list[Finding]:
    found: list[Finding] = []
    for ks, error in sorted(cc.head.errors.items()):
        if ks in cc.base.errors:
            found.append(
                cc.finding("GC001", Severity.WARN, ks, f"still does not build, as before: {error}")
            )
        else:
            found.append(
                cc.finding(
                    "GC001",
                    Severity.BLOCK,
                    ks,
                    f"does not build after this change: {error}. Flux stops applying it, and "
                    "whatever depends on it waits.",
                )
            )
    for ks in sorted(cc.base.errors):
        if ks in cc.head.inventories:
            found.append(
                cc.finding(
                    "GC008",
                    Severity.INFO,
                    ks,
                    "did not build before this change, so what it deletes cannot be known; "
                    "everything it builds now is counted as added",
                )
            )
    for ks, why in sorted(cc.head.foreign.items()):
        found.append(cc.finding("GC005", Severity.INFO, ks, f"not checked: {why}"))
    return found


def _ks_ref(namespace: str, name: str) -> Ref:
    return Ref(FLUX_KUSTOMIZE, "Kustomization", namespace, name)


def _dependencies(cc: ClusterCheck) -> list[Finding]:
    found: list[Finding] = []
    graph: dict[Ref, list[Ref]] = {}
    for ks, doc in sorted(cc.head.kustomizations.items()):
        graph[ks] = []
        for dep in dig(doc, "spec.dependsOn") or []:
            target = _ks_ref(str(dep.get("namespace") or ks.namespace), str(dep.get("name")))
            graph[ks].append(target)
            if target not in cc.head.kustomizations:
                found.append(_missing(cc, ks, target))
    for ref, doc in sorted(cc.head_objects.items()):
        if ref.group != FLUX_HELM or ref.kind != "HelmRelease":
            continue
        for dep in dig(doc, "spec.dependsOn") or []:
            target = Ref(
                FLUX_HELM,
                "HelmRelease",
                str(dep.get("namespace") or ref.namespace),
                str(dep.get("name")),
            )
            if target not in cc.head_objects:
                found.append(_missing(cc, ref, target))
    for cycle in _cycles(graph):
        names = " → ".join(str(ks) for ks in [*cycle, cycle[0]])
        new = any(ks in cc.touched for ks in cycle)
        found.append(
            cc.finding(
                "GC004",
                Severity.BLOCK if new else Severity.WARN,
                cycle[0],
                f"dependsOn goes round: {names}. None of them will ever be applied.",
            )
        )
    return found


def _missing(cc: ClusterCheck, ref: Ref, target: Ref) -> Finding:
    new = ref in cc.touched or target in cc.outcome.deleted
    return cc.finding(
        "GC003",
        Severity.BLOCK if new else Severity.WARN,
        ref,
        f"depends on {target}, which this cluster does not have: it waits for it for ever",
    )


def _cycles(graph: dict[Ref, list[Ref]]) -> list[list[Ref]]:
    cycles: list[list[Ref]] = []
    state: dict[Ref, int] = {}
    stack: list[Ref] = []

    def visit(node: Ref) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                cycles.append(stack[stack.index(nxt) :])
            elif nxt in graph and state.get(nxt) is None:
                visit(nxt)
        stack.pop()
        state[node] = 2

    for node in sorted(graph):
        if state.get(node) is None:
            visit(node)
    return cycles


def _owners(cc: ClusterCheck) -> list[Finding]:
    found: list[Finding] = []
    for ref, owners in sorted(cc.head.owners().items()):
        if len(owners) > 1:
            names = ", ".join(str(ks) for ks in owners)
            found.append(
                cc.finding(
                    "GC007",
                    Severity.WARN,
                    ref,
                    f"is applied by {names}: they overwrite each other, and when one of them "
                    "stops building it, it may delete it from under the other",
                )
            )
    return found


SOURCE_KINDS = {"GitRepository", "OCIRepository", "HelmRepository", "Bucket", "HelmChart"}


def _sources(cc: ClusterCheck) -> list[Finding]:
    found: list[Finding] = []
    known = {ref for ref in cc.head_objects if ref.group == FLUX_SOURCE}
    known |= {
        Ref(FLUX_SOURCE, ref.kind, ref.namespace, ref.name)
        for ref in cc.base_objects
        if ref.group == FLUX_SOURCE and ref not in cc.outcome.deleted
    }
    for ref, doc in sorted(cc.head_objects.items()):
        if ref not in cc.touched:
            continue
        wanted: dict[str, object] | None = None
        if ref.group == FLUX_HELM and ref.kind == "HelmRelease":
            wanted = dig(doc, "spec.chart.spec.sourceRef") or dig(doc, "spec.chartRef")
        elif ref.is_flux_kustomization:
            wanted = dig(doc, "spec.sourceRef")
        if not isinstance(wanted, dict) or wanted.get("kind") not in SOURCE_KINDS:
            continue
        source = Ref(
            FLUX_SOURCE,
            str(wanted["kind"]),
            str(wanted.get("namespace") or ref.namespace),
            str(wanted.get("name")),
        )
        if source not in known:
            found.append(
                cc.finding(
                    "GC009",
                    Severity.WARN,
                    ref,
                    f"reads {source}, which this cluster's Git does not define. If nothing "
                    "outside Git creates it, Flux will wait for it for ever.",
                )
            )
    return found


NOT_APPLIED_GROUPS = (KUSTOMIZE_CONFIG,)


def _orphans(cc: ClusterCheck) -> list[Finding]:
    """A manifest added or changed under the cluster that no Kustomization picks up."""
    applied = {(ref.kind, ref.name) for ref in cc.head_objects}
    inputs = referenced_non_resources(cc.files.head)
    found: list[Finding] = []
    for item in cc.files.head:
        if item.file not in cc.files.changed or not cc.deploys(item.file):
            continue
        if item.file in inputs or str(item.doc.get("apiVersion")).startswith(NOT_APPLIED_GROUPS):
            continue
        ref = Ref.of(item.doc)
        if (ref.kind, ref.name) in applied or not ref.name:
            continue
        found.append(
            Finding(
                "GC006",
                Severity.WARN,
                cc.name,
                f"{item.file}:{ref}",
                f"{ref} in {item.file} is not applied by any Kustomization: is it missing "
                "from a kustomization.yaml?",
                item.file,
                item.line,
            )
        )
    return found
