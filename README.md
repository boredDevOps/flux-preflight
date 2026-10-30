# flux-preflight

Checks a merge request into a Flux repository **before** it is merged. It reports what Flux
would delete or break in each cluster when the merge lands.

It renders every cluster twice, once from the target branch and once from the merge request,
exactly as Flux builds it (`flux build kustomization --dry-run`: the Flux Kustomization's own
patches, target namespace and substitutions included). It starts from each `gotk-sync.yaml` and
follows every Kustomization it applies. Then it runs Flux's garbage collection on the
difference: what each Kustomization stops building and prunes, and what a deleted Kustomization
takes with it, down the whole cascade.

It never talks to a cluster (`KUBECONFIG` points at nothing) and needs no secrets.

## What it checks

| Rule | Severity | What |
|---|---|---|
| GC001 | block | a Kustomization no longer builds (Flux stops applying it) |
| GC002 | block | YAML that does not parse, or has a duplicate key (YAML silently keeps the last one) |
| GC003 | block | `dependsOn` names a Kustomization or HelmRelease that does not exist |
| GC004 | block | `dependsOn` goes round in a circle |
| GC101 | block | Flux deletes a Namespace (and all in it), a CRD (and all its objects), a PVC/PV, StatefulSet, StorageClass, HelmRelease (= `helm uninstall`), a Flux Kustomization (with everything it applied) or source, or anything in `flux-system` |
| GC103 | block | Flux deletes more than `max_deletions` objects, or more than `max_deletion_ratio` of one Kustomization ([limits](#deletion-limits)) |
| GC104 | block | a protected object moves to another Kustomization (it may be deleted and recreated) |
| GC201 | block | a field Kubernetes refuses to change: selectors, StatefulSet `volumeClaimTemplates`/`serviceName`, PVC class/modes/shrink, `roleRef`, immutable ConfigMaps/Secrets, CRD scope, a CRD version dropped |
| GC202 | block | a HelmRelease becomes another release (`releaseName`/`targetNamespace`/`storageNamespace`) |
| GC301 | block | a Secret added or changed in plain text (not SOPS-encrypted) |
| GC307 | block | an object that does not match its schema, strictly: a misspelt field is an error |
| GC102 | warn | Flux deletes some other object |
| GC105 | warn | removed from Git but kept in the cluster (`prune: disabled`): delete it by hand |
| GC006 | warn | a manifest under a cluster that no Kustomization applies |
| GC007 | warn | two Kustomizations apply the same object |
| GC009 | warn | a chart or Kustomization reads a source the cluster's Git does not define |
| GC203 | warn | a HelmRelease changes chart or source, or moves to a new major version |
| GC204 | warn | something is suspended or scaled to 0 |
| GC302 | warn | a value under a password/token-like key written in Git (the value is never printed) |
| GC303 | warn | an image without a pinned version (`latest`, no tag) |
| GC304 | warn | a chart version that is a range or missing |
| GC305 | warn | a deprecated or removed Kubernetes or Flux API version |
| GC306 | warn | YAML lint: `yes`/`on` (booleans to Kubernetes), implicit octal numbers |
| GC005, GC008, GC205, GC308, GC309 | info | what could not be checked, and why |

A merge request answers for what it adds or changes. Problems already in Git (old plain-text
Secrets, old API versions, schema errors) are listed once as information. They do not block
unrelated work.

Nothing is skipped silently. A Kustomization reading another source, a schema that could not be
downloaded, or a chart's contents (HelmRelease values are checked, the charts are not rendered)
is listed under "Not checked".

## Accepting a finding

When a deletion is intended, add the exact line the report gives to the merge request's
description, for example:

```
flux-preflight allow GC101 production:Namespace/shop
```

That accepts only this finding. Anything else the merge request does is still checked.

## In GitLab CI

The steps, the settings around the job and what to do when it fails are in
[docs/gitlab.md](docs/gitlab.md). The job itself, in `.gitlab-ci.yml`:

```yaml
flux-preflight:
  stage: test
  image: ghcr.io/boreddevops/flux-preflight:v1   # the major version: fixes arrive, rules do not change under you
  variables:
    GIT_DEPTH: 0            # the merge request's base commit must be in the clone
  script:
    - flux-preflight --out flux-preflight
  artifacts:
    when: always
    paths: [flux-preflight/]
    reports:
      codequality: flux-preflight/code-quality.json   # findings on the merge request's Reports tab
  allow_failure:
    exit_codes: [3]         # warn mode: yellow, the merge is not blocked
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
```

To have the report as a note on the merge request, give the job a token with `api` scope as a
masked, unprotected variable `FLUX_PREFLIGHT_GITLAB_TOKEN`. Without it, the report is in the job
log, the artifacts and the Code Quality report.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | nothing blocks |
| 1 | something blocks (`mode: block`) |
| 3 | something would block (`mode: warn`) |
| 2 | the check itself could not run; that is never read as "nothing found" |

To block for real, set `mode: block` and turn on *Pipelines must succeed* in the project's
merge request settings.

## Configuration

`.flux-preflight.yaml` in the root of the checked repository. The file and every field in it are
optional, and a field you leave out or leave empty keeps its default. A value you set always wins
over the default, including `0`, and `--mode` on the command line wins over the file.

A field the check does not know or a value it cannot use, at any level, stops it with exit code 2
and says which one. A misspelt limit never silently falls back to the default.

```yaml
mode: warn
ignore: [no-run/]
clusters:
  - sync: clusters/production/flux-system/gotk-sync.yaml
    name: production
limits:
  max_deletions: 10
  max_deletion_ratio: 0.2
  min_objects_for_ratio: 5
  min_secret_length: 6
timeouts:
  flux_build_seconds: 180
  kubeconform_seconds: 600
  yamllint_seconds: 60
kubernetes_version: "1.32.0"
schemas:
  - default
  - https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json
schema_check: true
```

| Field | Default | What it does |
|---|---|---|
| `mode` | `warn` | What a blocking finding does. `warn`: exit code 3, the merge is not stopped. `block`: exit code 1. |
| `ignore` | none | Paths no cluster deploys: a file, or a directory with everything under it. Patterns like `no-*` do not work. These files get no YAML, Secret or lint checks, and no `gotk-sync.yaml` is looked for in them. A Kustomization that applies an ignored path is still rendered and checked. |
| `clusters` | every `gotk-sync.yaml` outside `ignore` | Replaces the search: only the clusters listed here are checked, even under an ignored path. |
| `clusters[].sync` | required | The file that holds the cluster's root Flux Kustomization. |
| `clusters[].name` | the root's `spec.path` | The cluster's name in reports and in `flux-preflight allow` lines. |
| `limits.max_deletions` | `10` | Whole number, `0` or more. See [Deletion limits](#deletion-limits). |
| `limits.max_deletion_ratio` | `0.2` | Number from `0` to `1`, where `0.2` means 20%. See [Deletion limits](#deletion-limits). |
| `limits.min_objects_for_ratio` | `5` | Whole number, `0` or more. See [Deletion limits](#deletion-limits). |
| `limits.min_secret_length` | `6` | Whole number, `0` or more. GC302 skips values shorter than this under a password-like key. |
| `timeouts.flux_build_seconds` | `180` | Whole number, `1` or more. The limit for one `flux build`, run per Kustomization on each side. A Kustomization that takes longer counts as not building (GC001). |
| `timeouts.kubeconform_seconds` | `600` | Whole number, `1` or more. The limit for one kubeconform run, up to two per cluster. Past it the schema check does not run, and the report says so as GC309. |
| `timeouts.yamllint_seconds` | `60` | Whole number, `1` or more. The limit for one yamllint run, up to two per changed file. Past it the check stops with exit code 2. |
| `kubernetes_version` | `"1.32.0"` | The Kubernetes version whose schemas GC307 uses, in quotes: an unquoted `1.30` is the number `1.3`. The check cannot read it from a cluster, so [set it to match yours](docs/gitlab.md#why-set-kubernetes_version). It applies to all clusters. |
| `schemas` | as in the example | Where kubeconform finds schemas. The CRDs in the cluster's own Git are always tried first. Your list replaces the default, so a mirror must also serve the Kubernetes schemas. `default` is kubeconform's own location. With `[]`, only those CRDs are checked and every other object is listed under GC308. |
| `schema_check` | `true` | `false` turns GC307 off. The report says so as GC309. |

Only clusters whose GitRepository follows the merge request's target branch are checked.

### Deletion limits

Rule GC103 stops a merge that deletes a lot at once, even when each object is unremarkable. Limits
are per cluster, and it fires on any of these:

- **The whole cluster:** Flux deletes more than `max_deletions` objects in all, including everything
  that goes with a deleted Kustomization.
- **One Kustomization, by count:** it stops building more than `max_deletions` of its own objects.
- **One Kustomization, by share:** it applies at least `min_objects_for_ratio` objects and stops
  building more than `max_deletion_ratio` of them. In a small Kustomization one object is already
  a large share, so below that size only the count applies.

"More than" means a value exactly at the limit passes:

| Change, with the defaults | GC103 |
|---|---|
| 10 objects deleted, and they are at most 20% of each Kustomization | no |
| 11 objects deleted | yes |
| 2 of a Kustomization's 10 objects (20%) | no |
| 3 of a Kustomization's 10 objects (30%) | yes |
| 1 of a Kustomization's 3 objects (33%) | no: fewer than 5 objects |

To turn a limit down or off:

| Setting | Effect |
|---|---|
| `max_deletions: 0` | Any deletion is GC103. |
| `max_deletion_ratio: 1` | The share never fires, only the count does. |
| `min_objects_for_ratio: 0` | The share applies to every Kustomization, however small. |

The limits do not soften other rules. Deleting one Namespace is GC101 whatever they are set to.

### Command line

| Option | Default | What it does |
|---|---|---|
| `--repo` | the current directory | The Flux repository. |
| `--base` | `$CI_MERGE_REQUEST_DIFF_BASE_SHA`, then `origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME` | What the change is compared with. Required outside a merge request pipeline. |
| `--head` | `HEAD` | The change. `WORKTREE` checks uncommitted files. |
| `--target-branch` | `$CI_MERGE_REQUEST_TARGET_BRANCH_NAME` | Only clusters that deploy from this branch are checked. Without it, all are. |
| `--mode` | `mode` from the file | Overrides it. |
| `--out` | `flux-preflight` | The directory for `report.md`, `report.json` and `code-quality.json`. |
| `--description-file` | `$CI_MERGE_REQUEST_DESCRIPTION` | A file to read `flux-preflight allow` lines from. |
| `--flux` | `$FLUX_BIN`, then `flux` | The Flux binary. |
| `--kubeconform` | `$KUBECONFORM_BIN`, then `kubeconform` | The kubeconform binary. |

### Environment variables

| Variable | Set by | What it does |
|---|---|---|
| `FLUX_PREFLIGHT_GITLAB_TOKEN` | you, optional | Lets the job write the report as a note on the merge request. |
| `FLUX_BIN`, `KUBECONFORM_BIN` | you, optional | Defaults for `--flux` and `--kubeconform`. |
| `CI_MERGE_REQUEST_DIFF_BASE_SHA`, `CI_MERGE_REQUEST_TARGET_BRANCH_NAME`, `CI_MERGE_REQUEST_DESCRIPTION` | GitLab | Defaults for the options above. |
| `CI_API_V4_URL`, `CI_PROJECT_ID`, `CI_MERGE_REQUEST_IID` | GitLab | Where the note goes. |

## Locally

```bash
docker build -t flux-preflight .
docker run --rm -v "$PWD":/repo:ro flux-preflight flux-preflight --repo /repo --base origin/prod --head WORKTREE --out /tmp/out
```

## Development

```bash
docker build --target test .
```

This runs ruff, mypy (strict) and pytest with the same `flux` and `kubeconform` binaries the image
ships. The tests build small Flux repositories in a temporary git and check merge requests
against them. Each rule has a change it must stop, and a look-alike it must let through.

## Releases

`.github/workflows/ci.yml` runs the test stage on every push and pull request. A tag `vX.Y.Z`
publishes `ghcr.io/boreddevops/flux-preflight:vX.Y.Z` and `:vX`, for `linux/amd64` and `linux/arm64`.
`:vX.Y.Z` never changes. `:vX` moves to the newest `vX` release. A new rule that can block is a new
major version, so a repository pinned to `:v1` is not blocked by surprise.

The first release needs one manual step, because GHCR starts every new package private, even from
a public repository. In the organization's settings, allow public packages under *Packages*. After
the first tag has pushed the image, open the package on GitHub, then *Package settings*, *Change
visibility*, *Public*, and re-run the failed job. A public package cannot be made private again.
From then on, CI checks on every release that the image can be pulled without logging in.

## License

Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). The Docker image also ships third-party
programs under their own licenses, among them yamllint (GPL-3.0-or-later), which runs as a separate
program: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
