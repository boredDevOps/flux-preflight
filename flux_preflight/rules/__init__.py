"""The rules. Each one reads the two renders (and the files) and returns findings.

Rule ids are stable: a merge request accepts a finding by its id and subject.
"""

RULES: dict[str, str] = {
    "GC001": "a Kustomization does not build",
    "GC002": "YAML that does not parse or has a duplicate key",
    "GC003": "dependsOn names something that does not exist",
    "GC004": "dependsOn goes round in a circle",
    "GC005": "not checked: reads another source",
    "GC006": "a manifest that nothing applies",
    "GC007": "two Kustomizations apply the same object",
    "GC008": "cannot compare: the Kustomization did not build before this change either",
    "GC009": "a source that is not in this cluster's Git",
    "GC101": "Flux deletes something whose loss is an outage or data loss",
    "GC102": "Flux deletes an object",
    "GC103": "Flux deletes many objects at once",
    "GC104": "an object moves to another Kustomization",
    "GC105": "removed from Git, but stays in the cluster",
    "GC201": "a field Kubernetes does not let change",
    "GC202": "a Helm release becomes another release",
    "GC203": "a Helm chart changes",
    "GC204": "something is switched off",
    "GC205": "a Flux Kustomization's spec changes",
    "GC206": "what a Kustomization applies from another repository changes, unseen",
    "GC301": "a Secret in Git in plain text",
    "GC302": "a value that looks like a password",
    "GC303": "an image without a pinned version",
    "GC304": "a chart without a pinned version",
    "GC305": "an API version that is deprecated or removed",
    "GC306": "YAML lint",
    "GC307": "an object that does not match its schema",
    "GC308": "objects no schema was found for",
    "GC309": "the schema check could not run",
}
