# Connecting flux-preflight to a GitHub repository

The check runs as one job in pull request workflows. Start in warn mode, where it reports but
stops nothing. Move to block mode once the reports match what you would have stopped by hand.

## Before you start

- **A Linux runner**, `linux/amd64` or `linux/arm64`. GitHub-hosted `ubuntu-*` runners work, and so
  do self-hosted Linux runners with Docker. The job runs in the image as a container, so it needs
  access to `ghcr.io`, but no registry credentials: the image is public.
- **Access to `raw.githubusercontent.com`**, where kubeconform downloads schemas. Without it the
  job still runs, and the objects it could not check against a schema are reported as `GC309`.
- **The Admin role** on the repository, for the branch rules in step 6. Steps 1 to 5 need Write.

The job needs no cluster access and no secrets from the cluster.

## 1. Add the configuration

`.flux-preflight.yaml` in the repository root. Every field is optional:

```yaml
mode: warn                  # reports, and stops nothing
ignore: [no-run/]           # paths no cluster deploys
```

By default each `gotk-sync.yaml` outside `ignore` is one cluster. The README
[describes every field](../README.md#configuration). Set `kubernetes_version` when your clusters
do not run `1.32`; [the GitLab guide explains why](gitlab.md#why-set-kubernetes_version), and it
is the same here.

## 2. Add the workflow

`.github/workflows/flux-preflight.yml`:

```yaml
name: flux-preflight

on:
  pull_request:
    types: [opened, synchronize, reopened, edited]

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: flux-preflight-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  flux-preflight:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    container: ghcr.io/boreddevops/flux-preflight:v1
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - name: Check what the merge deletes or breaks
        env:
          FLUX_PREFLIGHT_GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: flux-preflight --out flux-preflight || [ $? -eq 3 ]
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: flux-preflight
          path: flux-preflight/
```

| Line | Why it is there |
|---|---|
| `types: [..., edited]` | Editing the description starts a new run, so an `allow` line (step 5) takes effect by itself. |
| `pull-requests: write` | Only for the comment on the pull request. Without the comment, `contents: read` is enough. |
| `concurrency` | A newer push cancels the run for the older one. |
| `container: ...:v1` | Follows the newest `v1` release: fixes arrive, new blocking rules do not. To pin an exact release, use its full tag, `vX.Y.Z`. |
| `fetch-depth: 0` | The commit the pull request is compared with must be in the clone. |
| `FLUX_PREFLIGHT_GITHUB_TOKEN` | Lets the job write the report as one comment on the pull request. Leave it out and there is no comment; everything else stays. |
| `\|\| [ $? -eq 3 ]` | Warn mode: exit code 3, "something would block", passes the job. Any other failure stays red. In block mode the check never exits with 3, so the line stays as it is. |
| `if: always()` | The reports are kept when the job fails, which is when you need them. |

The check reads the pull request from the event GitHub writes for the run. Do not pass the
description through `${{ github.event.pull_request.body }}` in a `run:` line: whoever writes the
description could then run commands in the job.

Do not use `pull_request_target`. It checks out the base branch, so there is nothing to compare,
and it runs the change with a token that can write. The check ignores that event.

## 3. Open a pull request and read the result

Put steps 1 and 2 on a branch and open a pull request. The first run tells you what the check sees
in this repository:

- **"Not checked"** lists what was left out. A path no cluster deploys is a candidate for `ignore`.
- **A cluster that is skipped** deploys from another branch. Only clusters whose GitRepository
  follows the pull request's base branch are checked.

The report is in four places, and the first three need no token:

| Where | What |
|---|---|
| The run's **Summary** page | The whole report. |
| **Files changed** in the pull request | Blocking findings as errors and warnings on the lines they are about. |
| The `flux-preflight` artifact | `report.md`, `report.json`, `code-quality.json`. |
| A comment on the pull request | The whole report, updated on each run. Only with the token. |

| Exit code | Meaning | In the workflow |
|---|---|---|
| 0 | nothing blocks | passed |
| 3 | something would block, in warn mode | passed, with a warning on the run |
| 1 | something blocks, in block mode | failed |
| 2 | the check itself could not run | failed: nothing was verified |

GitHub has no "passed with warnings" state for a job, so in warn mode a would-block run is green.
The headline on the Summary page, the comment and a warning on the run say that something would
have blocked.

## 4. What is compared

`actions/checkout` checks out a merge of the pull request into the base branch as it is now
(`refs/pull/N/merge`). The check compares that merge with the base branch, so it sees exactly
what the merge brings, including how it combines with changes that reached the base branch
since the branch was cut.

If you check out the pull request's own commit instead (`ref: ${{ github.event.pull_request.head.sha }}`),
it is compared with the commit where it left the base branch, as in GitLab.

A pull request with merge conflicts gets no run at all until they are resolved: GitHub does not
start `pull_request` workflows without a merge commit.

## 5. Accepting a finding

When a change is intended, copy the line the report gives into the pull request's description:

```
flux-preflight allow GC101 production:Namespace/shop
```

Saving the description starts a new run, and that run reads the new description. *Re-run jobs* on
an old run does not help: it replays the old event, with the old description.

## 6. Moving to block mode

When the reports have been right for a while:

1. Set `mode: block` in `.flux-preflight.yaml`.
2. Make the check required: *Settings > Rules > Rulesets*, a branch ruleset for the base branch
   with *Require status checks to pass*, and add the check `flux-preflight`. A branch protection
   rule with *Require status checks to pass before merging* works the same way.

Now exit code 1 stops the merge. So does exit code 2, on purpose: a check that could not run has
verified nothing.

The check is listed by the job's name. If you rename the job, change the required check too, or
the merge waits for a check that never comes.

## When it fails

| What you see | Cause | Fix |
|---|---|---|
| `no --base and not a merge request pipeline` | The workflow ran on another event than `pull_request`. | Use the `on:` from step 2. |
| `no common commit of ... in this clone` or `the base ... is not in this clone` | A shallow clone. | Set `fetch-depth: 0` on `actions/checkout`. |
| `.flux-preflight.yaml: ...` | A misspelt field or a value that cannot be used. The message names it. | Fix the field. |
| `flux build took longer than` | A Kustomization takes longer to build than the job allows. | Raise `timeouts.flux_build_seconds`. |
| No comment on the pull request | No token reached the job. | Set `FLUX_PREFLIGHT_GITHUB_TOKEN` as in step 2. |
| `could not write the comment: HTTP Error 403` | The token cannot write: `pull-requests: write` is missing, or the pull request comes from a fork. | Add the permission. For forks, see below. |
| `GC309`: a schema could not be downloaded | No access to the schema host. | Allow the host, or set `schemas:` to a mirror. |
| The job cannot pull the image | The runner has no access to `ghcr.io`. | Allow the host, or copy the image to your own registry and change `container:`. |

## Good to know

- **Pull requests from forks** get a token that cannot write, so there is no comment. The Summary
  page, the annotations and the artifact still have the whole report, and the verdict does not
  change. GitHub may also wait for a maintainer to approve the run.
- **Annotations are capped by GitHub**, at 10 errors and 10 warnings per step. The Summary page and
  the comment list everything.
- **GitHub Enterprise Server** works the same way: the comment goes to the server's own API.
- **Charts are not rendered.** HelmRelease values are checked, but what a chart version deletes
  is not. It is listed under "Not checked".
- **A Kustomization that reads another repository is not rendered.** Deleting it still blocks, and
  moving its source to another tag warns as `GC206`, but read that repository's changes yourself.
  [The README lists what is reported](../README.md#a-kustomization-that-reads-another-repository).
