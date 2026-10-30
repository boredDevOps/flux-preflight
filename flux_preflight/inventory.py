"""What Flux's garbage collection does when the merge lands.

An object leaves the cluster when its Kustomization prunes and no longer builds it, or when its
Kustomization is itself deleted (then every object it applied goes, unless marked
`kustomize.toolkit.fluxcd.io/prune: disabled`). A deleted Kustomization deletes the ones it
applied in turn, so the cascade is followed from the root down. An object another Kustomization
now builds is moved, not deleted, but only if the new owner applies it first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flux_preflight.model import Doc, Ref, Render, canonical, dig, prune_disabled


@dataclass
class Deletion:
    ref: Ref
    doc: Doc
    owner: Ref
    # The deleted Kustomization that takes it along; None when its own Kustomization drops it.
    via: Ref | None


@dataclass
class Move:
    ref: Ref
    doc: Doc
    old_owner: Ref
    new_owner: Ref


@dataclass
class Kept:
    ref: Ref
    owner: Ref
    reason: str


@dataclass
class Outcome:
    deletions: list[Deletion] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    kept: list[Kept] = field(default_factory=list)
    added: list[Ref] = field(default_factory=list)
    changed: list[Ref] = field(default_factory=list)
    # Kustomizations whose result after the merge cannot be known (build failed, other source).
    unknown: list[Ref] = field(default_factory=list)

    @property
    def deleted(self) -> set[Ref]:
        return {item.ref for item in self.deletions}

    def taken_by(self, ks: Ref) -> list[Deletion]:
        """Everything a deleted Kustomization takes with it, its children's objects included."""
        found: list[Deletion] = []
        stack = [ks]
        while stack:
            via = stack.pop()
            for item in self.deletions:
                if item.via == via:
                    found.append(item)
                    if item.ref.is_flux_kustomization:
                        stack.append(item.ref)
        return found


def _prunes(ks: Doc) -> bool:
    return dig(ks, "spec.prune") is True


def compare(base: Render, head: Render) -> Outcome:
    outcome = Outcome()
    head_owners = head.owners()
    base_objects = base.objects()
    head_objects = head.objects()
    for ref, doc in head_objects.items():
        if ref not in base_objects:
            outcome.added.append(ref)
        elif canonical(doc) != canonical(base_objects[ref]):
            outcome.changed.append(ref)

    gone: set[Ref] = set()
    root = base.root
    root_doc = base.inventories.get(root, {}).get(root)
    if (
        root_doc is not None
        and root in head.inventories
        and root not in head.inventories[root]
        and root not in head_owners
        and _prunes(base.kustomizations[root])
        and not prune_disabled(root_doc)
    ):
        # The root no longer builds itself: Flux deletes its own root, and all below it.
        gone.add(root)
        outcome.deletions.append(Deletion(root, root_doc, root, via=None))

    for ks in base.order():
        inventory = base.inventories.get(ks)
        if inventory is None:
            continue
        if ks in gone:
            spec = base.kustomizations[ks]
            dropped = dict(inventory)
            dropped.pop(ks, None)
        elif ks in head.inventories:
            spec = head.kustomizations[ks]
            dropped = {r: d for r, d in inventory.items() if r not in head.inventories[ks]}
        elif ks in head.errors or ks in head.foreign:
            outcome.unknown.append(ks)
            continue
        else:
            # Still in the cluster but nothing applies it any more (its parent keeps it):
            # it goes on reconciling its last spec, so its objects stay.
            continue
        for ref, doc in sorted(dropped.items()):
            via = ks if ks in gone else None
            owners = head_owners.get(ref)
            new_owner = owners[0] if owners else None
            if new_owner is not None and new_owner != ks and new_owner not in gone:
                outcome.moves.append(Move(ref, doc, ks, new_owner))
            elif not _prunes(spec):
                outcome.kept.append(Kept(ref, ks, "its Kustomization does not prune"))
            elif prune_disabled(doc):
                outcome.kept.append(Kept(ref, ks, "it is marked prune: disabled"))
            else:
                outcome.deletions.append(Deletion(ref, doc, ks, via))
                if ref.is_flux_kustomization:
                    gone.add(ref)
    return outcome
