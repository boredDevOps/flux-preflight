# Connecting flux-preflight to a GitLab project

The check runs as one job in merge request pipelines. Start in warn mode, where it reports but
stops nothing. Move to block mode once the reports match what you would have stopped by hand.

## Before you start

- **A runner** with the Docker or Kubernetes executor, on `linux/amd64` or `linux/arm64`. The image
  is public, so it needs no registry credentials, only access to `ghcr.io`.
- **Access to `raw.githubusercontent.com`**, where kubeconform downloads schemas. Without it the
  job still runs, and the objects it could not check against a schema are reported as `GC309`.
- **The Maintainer role** on the project, for the merge request settings and the variable.

The job needs no cluster access and no secrets from the cluster.

## 1. Add the configuration

`.flux-preflight.yaml` in the repository root. Every field is optional:

```yaml
mode: warn                  # reports, and stops nothing
ignore: [no-run/]           # paths no cluster deploys
```

By default each `gotk-sync.yaml` outside `ignore` is one cluster. The README
[describes every field](../README.md#configuration).

### Why set `kubernetes_version`

Rule `GC307` checks every object against the schemas of one Kubernetes version. The check never
talks to a cluster, so it cannot find out which version you run, and it uses `1.32.0` unless you
tell it otherwise. Set the field when your clusters run another minor version:

```yaml
kubernetes_version: "1.30.0"
```

A value that does not match your clusters makes the check wrong in one of two ways:

- **Too old:** a field your cluster supports is reported as an error, and in block mode that stops
  the merge.
- **Too new:** a field your cluster does not support yet passes, and the apply fails after the merge.

The value applies to all clusters in the repository. Update it when you upgrade the clusters.

## 2. Add the job

`.gitlab-ci.yml`:

```yaml
flux-preflight:
  stage: test
  image: ghcr.io/boreddevops/flux-preflight:v1
  variables:
    GIT_DEPTH: 0
  script:
    - flux-preflight --out flux-preflight
  artifacts:
    when: always
    paths: [flux-preflight/]
    reports:
      codequality: flux-preflight/code-quality.json
  allow_failure:
    exit_codes: [3]
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
```

| Line | Why it is there |
|---|---|
| `stage: test` | Use a stage your pipeline defines. |
| `image: ...:v1` | Follows the newest `v1` release: fixes arrive, new blocking rules do not. To pin an exact release, use its full tag, `vX.Y.Z`. |
| `GIT_DEPTH: 0` | The merge request's base commit must be in the clone. |
| `when: always` | The reports are kept when the job fails, which is when you need them. |
| `exit_codes: [3]` | Warn mode: would-block findings show as a warning and the pipeline passes. Any other failure stays red. |
| `rules` | The check reads the merge request's variables, so it only runs in merge request pipelines. |

**If your other jobs run in branch pipelines**, this job adds a second pipeline to each merge
request. To have one pipeline per merge request, add this, and check that your other jobs have
rules that match merge request pipelines:

```yaml
workflow:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH && $CI_OPEN_MERGE_REQUESTS
      when: never
    - if: $CI_COMMIT_BRANCH
```

## 3. Open a merge request and read the result

Put steps 1 and 2 on a branch and open a merge request. The first run tells you what the check
sees in this repository:

- **"Not checked"** lists what was left out. A path no cluster deploys is a candidate for `ignore`.
- **A cluster that is skipped** deploys from another branch. Only clusters whose GitRepository
  follows the merge request's target branch are checked.

The report is in three places: the job log, the `flux-preflight/` artifacts (`report.md`,
`report.json`) and the merge request's **Reports** tab through Code Quality.

| Exit code | Meaning | In the pipeline |
|---|---|---|
| 0 | nothing blocks | passed |
| 3 | something would block, in warn mode | warning, the pipeline passes |
| 1 | something blocks, in block mode | failed |
| 2 | the check itself could not run | failed: nothing was verified |

## 4. Optional: a note on the merge request

With a token, the job writes the report as one note on the merge request and updates that note on
each run.

1. Create a token with `api` scope and the Reporter role. A project access token is the best fit.
   It is available on self-managed GitLab with any license, and on GitLab.com from Premium. On
   GitLab.com Free, use a personal access token of a dedicated user.
2. Add it under *Settings > CI/CD > Variables* as `FLUX_PREFLIGHT_GITLAB_TOKEN`, with **Masked** on
   and **Protected off**.

Protected variables do not reach pipelines from unprotected source branches. With Protected on,
the job finds no token and writes no note, without any error.

The cost of Protected off: anyone who can push a branch can change the pipeline and read the token.
That is why this step is optional, and why the token should belong to this project only. If the
note cannot be written, the job log says why and the verdict does not change.

## 5. Accepting a finding

When a change is intended, copy the line the report gives into the merge request's description:

```
flux-preflight allow GC101 production:Namespace/shop
```

Then start a **new pipeline**: the merge request's *Pipelines* tab, *Run pipeline*. Two things to
know:

- Editing a description does not start a pipeline, and retrying the old job is not enough, because
  it runs in the old pipeline's context.
- GitLab passes only the first 2700 characters of the description to the job. Put these lines near
  the top. A line past that limit is not seen, so the finding stays.

## 6. Moving to block mode

When the reports have been right for a while:

1. Set `mode: block` in `.flux-preflight.yaml`.
2. Turn on *Settings > Merge requests > Merge checks > Pipelines must succeed*.

Now exit code 1 stops the merge. So does exit code 2, on purpose: a check that could not run has
verified nothing.

## When it fails

| What you see | Cause | Fix |
|---|---|---|
| `no --base and not a merge request pipeline` | The job ran in a branch pipeline. | Use the `rules` from step 2. |
| `the base ... is not in this clone and could not be fetched` | The clone does not have the base commit. | Set `GIT_DEPTH: 0`. |
| `.flux-preflight.yaml: ...` | A misspelt field or a value that cannot be used. The message names it. | Fix the field. |
| `flux build took longer than` | A Kustomization takes longer to build than the job allows. | Raise `timeouts.flux_build_seconds`. |
| No note on the merge request | No token reached the job. | Check that the variable is not Protected. |
| `could not write the note` | The token was refused or GitLab was unreachable. | Check the token's scope, role and expiry. |
| `GC309`: a schema could not be downloaded | No access to the schema host. | Allow the host, or set `schemas:` to a mirror. |
| The job cannot pull the image | The runner has no access to `ghcr.io`. | Allow the host, or copy the image to your own registry and change `image:`. |

## Good to know

- **Merge requests from forks** run in the fork, with the fork's variables, so there is no note. A
  member of the parent project can run the pipeline in the parent from the *Pipelines* tab.
- **Merged results pipelines** check the merge result against the merge request's base commit, so
  changes that reached the target branch after the branch was cut can show up as this merge
  request's. This setup has not been tested with them.
- **Charts are not rendered yet.** HelmRelease values are checked, but what a chart version deletes
  is not. It is listed under "Not checked".
