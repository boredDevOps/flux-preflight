"""Merge requests against a small Flux repository, rendered by the real flux binary.

Each rule has a change it must stop and, where it matters, a look-alike it must let through:
a check that blocks harmless work gets switched off, one that misses gets trusted wrongly.
"""

from __future__ import annotations

import pytest

from tests.conftest import Repo

Cap = pytest.CaptureFixture[str]


def test_a_new_image_version_passes(repo: Repo, capsys: Cap) -> None:
    repo.edit("apps/web.yaml", "nginx:1.27.0", "nginx:1.27.1")
    checked = repo.check(capsys)
    assert checked.code == 0
    assert checked.rules("block") == [] and checked.rules("warn") == []
    assert checked.cluster["changed"] == 1 and checked.cluster["deleted"] == 0
    assert checked.report["verdict"] == "pass"


def test_a_root_kustomization_that_leaves_parts_out_is_stopped(repo: Repo, capsys: Cap) -> None:
    """The outage this was made for: a kustomization.yaml at the root lists only some of it."""
    repo.write(
        {
            "clusters/prod/kustomization.yaml": (
                "apiVersion: kustomize.config.k8s.io/v1beta1\n"
                "kind: Kustomization\nresources:\n  - flux-system\n"
            )
        }
    )
    checked = repo.check(capsys)
    assert checked.code == 3  # warn mode: would block
    apps = checked.find("GC101", "Kustomization/flux-system/apps")
    assert "Namespace/shop" in apps["message"]
    data = checked.find("GC101", "Kustomization/flux-system/data")
    assert "HelmRelease/shop/redis" in data["message"]
    assert "StatefulSet/shop/db" in data["message"]
    # Its objects are told under the Kustomization, not one finding each.
    assert not any(f["subject"] == "Deployment/shop/web" for f in checked.report["findings"])
    assert checked.cluster["deleted"] == 8
    assert "❌" in checked.markdown and "flux-preflight allow GC101" in checked.markdown


def test_the_root_dropping_itself_takes_the_whole_cluster(repo: Repo, capsys: Cap) -> None:
    repo.write(
        {
            "clusters/prod/flux-system/kustomization.yaml": (
                "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: []\n"
            )
        }
    )
    checked = repo.check(capsys)
    root = checked.find("GC101", "Kustomization/flux-system/flux-system")
    assert "builds itself" in root["message"]
    assert checked.cluster["deleted"] == 10


def test_a_namespace_left_out_says_what_goes_with_it(repo: Repo, capsys: Cap) -> None:
    repo.edit("apps/kustomization.yaml", "  - namespace.yaml\n", "")
    checked = repo.check(capsys)
    found = checked.find("GC101", "Namespace/shop")
    assert "everything in it: 4 objects from Git" in found["message"]
    assert found["path"] == "apps/namespace.yaml"


def test_a_namespace_marked_prune_disabled_stays_and_says_so(repo: Repo, capsys: Cap) -> None:
    repo.edit(
        "apps/namespace.yaml",
        "  name: shop\n",
        "  name: shop\n  annotations:\n    kustomize.toolkit.fluxcd.io/prune: disabled\n",
    )
    repo.base = repo.commit("protect the namespace")
    repo.edit("apps/kustomization.yaml", "  - namespace.yaml\n", "")
    checked = repo.check(capsys)
    assert checked.rules("block") == []
    assert "stays in the cluster" in checked.find("GC105", "Namespace/shop")["message"]


def test_a_kustomization_dropped_but_kept_by_its_parent_deletes_nothing(
    repo: Repo, capsys: Cap
) -> None:
    repo.edit(
        "clusters/prod/kustomizations.yaml",
        "  name: data\n  namespace: flux-system\n",
        "  name: data\n  namespace: flux-system\n  annotations:\n"
        "    kustomize.toolkit.fluxcd.io/prune: disabled\n",
    )
    repo.base = repo.commit("protect data")
    text = (repo.path / "clusters/prod/kustomizations.yaml").read_text()
    repo.write({"clusters/prod/kustomizations.yaml": text.split("---")[0]})
    checked = repo.check(capsys)
    assert checked.cluster["deleted"] == 0
    assert "GC105" in checked.rules("warn")


def test_one_configmap_removed_is_a_warning_not_a_block(repo: Repo, capsys: Cap) -> None:
    repo.edit("apps/kustomization.yaml", "  - config.yaml\n", "")
    checked = repo.check(capsys)
    assert checked.code == 0
    assert checked.rules("warn") == ["GC102"]


def test_many_deletions_at_once_are_stopped(repo: Repo, capsys: Cap) -> None:
    configs = "".join(
        f"---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c{i}\n  namespace: shop\n"
        for i in range(12)
    )
    repo.write({"apps/many.yaml": configs})
    repo.edit("apps/kustomization.yaml", "  - config.yaml\n", "  - config.yaml\n  - many.yaml\n")
    repo.base = repo.commit("many configs")
    repo.edit("apps/kustomization.yaml", "  - many.yaml\n", "")
    checked = repo.check(capsys)
    assert (
        "Flux deletes 12 of the"
        in checked.find("GC103", "Kustomization/flux-system/apps")["message"]
    )


def test_a_selector_change_is_stopped(repo: Repo, capsys: Cap) -> None:
    repo.edit("apps/web.yaml", "      app: web\n  template", "      app: web2\n  template")
    repo.edit("apps/web.yaml", "        app: web\n    spec", "        app: web2\n    spec")
    checked = repo.check(capsys)
    assert "spec.selector" in checked.find("GC201", "Deployment/shop/web")["message"]


def test_a_volume_claim_template_change_is_stopped(repo: Repo, capsys: Cap) -> None:
    repo.edit("data/db.yaml", "storage: 10Gi", "storage: 20Gi")
    checked = repo.check(capsys)
    assert "volumeClaimTemplates" in checked.find("GC201", "StatefulSet/shop/db")["message"]


def test_a_helm_release_renamed_is_stopped(repo: Repo, capsys: Cap) -> None:
    repo.edit("data/redis.yaml", "spec:\n  interval", "spec:\n  releaseName: cache\n  interval")
    checked = repo.check(capsys)
    assert (
        "uninstalls the old release" in checked.find("GC202", "HelmRelease/shop/redis")["message"]
    )


def test_a_chart_major_bump_and_a_range_are_warnings(repo: Repo, capsys: Cap) -> None:
    repo.edit("data/redis.yaml", "version: 19.6.4", "version: '>=20.0.0'")
    checked = repo.check(capsys)
    assert checked.rules("block") == []
    assert {"GC304"} <= set(checked.rules("warn"))


def test_a_kustomization_that_does_not_build_is_stopped(repo: Repo, capsys: Cap) -> None:
    repo.edit("apps/kustomization.yaml", "  - web.yaml\n", "  - web.yaml\n  - missing.yaml\n")
    checked = repo.check(capsys)
    found = checked.find("GC001", "Kustomization/flux-system/apps")
    assert "missing.yaml" in found["message"]


def test_a_dependency_that_does_not_exist_is_stopped(repo: Repo, capsys: Cap) -> None:
    repo.edit("clusters/prod/kustomizations.yaml", "    - name: apps\n", "    - name: appz\n")
    checked = repo.check(capsys)
    assert "Kustomization/flux-system/appz" in checked.find("GC003", "data")["message"]


def test_a_namespace_moved_between_kustomizations_is_stopped(repo: Repo, capsys: Cap) -> None:
    repo.edit("apps/kustomization.yaml", "  - namespace.yaml\n", "")
    repo.write({"data/namespace.yaml": (repo.path / "apps/namespace.yaml").read_text()})
    repo.edit("data/kustomization.yaml", "  - db.yaml\n", "  - db.yaml\n  - namespace.yaml\n")
    checked = repo.check(capsys)
    assert checked.cluster["deleted"] == 0
    assert "moves from" in checked.find("GC104", "Namespace/shop")["message"]
    assert "GC104" in checked.rules("block")


def test_a_plain_text_secret_is_stopped_and_its_value_never_printed(
    repo: Repo, capsys: Cap
) -> None:
    repo.write(
        {
            "apps/secret.yaml": (
                "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\n  namespace: shop\n"
                "stringData:\n  password: hunter2-very-secret\n"
            )
        }
    )
    repo.edit("apps/kustomization.yaml", "  - config.yaml\n", "  - config.yaml\n  - secret.yaml\n")
    checked = repo.check(capsys)
    assert "password" in checked.find("GC301", "Secret/shop/db")["message"]
    for said in (checked.markdown, checked.text, str(checked.report)):
        assert "hunter2" not in said


def test_a_password_in_helm_values_is_a_warning_without_its_value(repo: Repo, capsys: Cap) -> None:
    repo.edit(
        "data/redis.yaml",
        "    architecture: standalone\n",
        "    architecture: standalone\n    auth:\n      password: s3cr3t-in-git\n",
    )
    checked = repo.check(capsys)
    assert "values.auth.password" in checked.find("GC302")["message"]
    assert "s3cr3t" not in checked.markdown + checked.text + str(checked.report)


def test_a_duplicate_key_is_stopped(repo: Repo, capsys: Cap) -> None:
    repo.edit("apps/web.yaml", "  replicas: 2\n", "  replicas: 2\n  replicas: 0\n")
    checked = repo.check(capsys)
    assert "duplicate key 'replicas'" in checked.find("GC002", "apps/web.yaml")["message"]


def test_yes_where_a_string_was_meant_is_a_warning(repo: Repo, capsys: Cap) -> None:
    repo.edit("apps/config.yaml", "mode: production", "mode: production\n  debug: yes")
    checked = repo.check(capsys)
    assert "GC306" in checked.rules("warn")


def test_a_manifest_nothing_applies_is_a_warning(repo: Repo, capsys: Cap) -> None:
    repo.write(
        {"apps/forgotten.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: forgotten\n"}
    )
    checked = repo.check(capsys)
    assert "missing from a kustomization.yaml" in checked.find("GC006", "forgotten")["message"]


def test_an_unpinned_image_is_a_warning(repo: Repo, capsys: Cap) -> None:
    repo.edit("apps/web.yaml", "nginx:1.27.0", "nginx:latest")
    checked = repo.check(capsys)
    assert "nginx:latest" in checked.find("GC303", "Deployment/shop/web")["message"]


def test_suspending_is_a_warning(repo: Repo, capsys: Cap) -> None:
    repo.edit("data/redis.yaml", "spec:\n  interval", "spec:\n  suspend: true\n  interval")
    checked = repo.check(capsys)
    assert "GC204" in checked.rules("warn")


def test_the_description_accepts_exactly_what_it_names(repo: Repo, capsys: Cap) -> None:
    repo.edit("apps/kustomization.yaml", "  - namespace.yaml\n", "")
    allow = "Retiring the shop.\n\nflux-preflight allow GC101 clusters/prod:Namespace/shop\n"
    checked = repo.check(capsys, description=allow)
    assert checked.code == 0
    assert checked.find("GC101", "Namespace/shop")["allowed"] is True
    assert "Accepted in the description" in checked.markdown


def test_block_mode_fails_the_job(repo: Repo, capsys: Cap) -> None:
    repo.edit("apps/kustomization.yaml", "  - namespace.yaml\n", "")
    assert repo.check(capsys, mode="block").code == 1


def test_a_cluster_on_another_branch_is_left_alone(repo: Repo, capsys: Cap) -> None:
    repo.edit("apps/kustomization.yaml", "  - namespace.yaml\n", "")
    checked = repo.check(capsys, target="develop")
    assert checked.code == 0
    assert "deploys from branch main" in checked.cluster["skipped"]


def test_a_misspelt_field_of_a_custom_resource_is_stopped(repo: Repo, capsys: Cap) -> None:
    crd = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.com
spec:
  group: example.com
  names: {kind: Widget, plural: widgets, singular: widget, listKind: WidgetList}
  scope: Namespaced
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                size: {type: integer}
"""
    repo.write({"apps/crd.yaml": crd})
    repo.edit("apps/kustomization.yaml", "  - config.yaml\n", "  - config.yaml\n  - crd.yaml\n")
    repo.base = repo.commit("add the CRD")
    repo.write(
        {
            "apps/widget.yaml": (
                "apiVersion: example.com/v1\nkind: Widget\nmetadata:\n  name: w\n"
                "  namespace: shop\nspec:\n  sise: 3\n"
            )
        }
    )
    repo.edit("apps/kustomization.yaml", "  - crd.yaml\n", "  - crd.yaml\n  - widget.yaml\n")
    checked = repo.check(capsys)
    assert "sise" in checked.find("GC307", "Widget/shop/w")["message"]


def test_dependencies_in_a_circle_are_stopped(repo: Repo, capsys: Cap) -> None:
    repo.edit(
        "clusters/prod/kustomizations.yaml",
        "  path: ./apps\n",
        "  path: ./apps\n  dependsOn:\n    - name: data\n",
    )
    checked = repo.check(capsys)
    assert "goes round" in checked.find("GC004")["message"]


def test_two_kustomizations_applying_one_object_is_a_warning(repo: Repo, capsys: Cap) -> None:
    repo.write({"data/web-config.yaml": (repo.path / "apps/config.yaml").read_text()})
    repo.edit("data/kustomization.yaml", "  - db.yaml\n", "  - db.yaml\n  - web-config.yaml\n")
    checked = repo.check(capsys)
    assert "overwrite each other" in checked.find("GC007", "ConfigMap/shop/web")["message"]


def test_a_chart_from_a_source_nobody_defines_is_a_warning(repo: Repo, capsys: Cap) -> None:
    repo.edit("data/redis.yaml", "        name: bitnami\n", "        name: bitnamy\n")
    checked = repo.check(capsys)
    assert "HelmRepository/flux-system/bitnamy" in checked.find("GC009")["message"]


def test_an_old_api_on_a_new_object_is_a_warning(repo: Repo, capsys: Cap) -> None:
    repo.edit(
        "data/repository.yaml", "source.toolkit.fluxcd.io/v1", "source.toolkit.fluxcd.io/v1beta1"
    )
    checked = repo.check(capsys)
    assert "deprecated" in checked.find("GC305", "HelmRepository/flux-system/bitnami")["message"]


def test_dropping_a_crd_version_is_stopped(repo: Repo, capsys: Cap) -> None:
    crd = (
        "apiVersion: apiextensions.k8s.io/v1\nkind: CustomResourceDefinition\nmetadata:\n"
        "  name: widgets.example.com\nspec:\n  group: example.com\n"
        "  names: {kind: Widget, plural: widgets}\n  scope: Namespaced\n  versions:\n"
        "    - {name: v1, served: true, storage: true}\n"
        "    - {name: v1beta1, served: true, storage: false}\n"
    )
    repo.write({"apps/crd.yaml": crd})
    repo.edit("apps/kustomization.yaml", "  - config.yaml\n", "  - config.yaml\n  - crd.yaml\n")
    repo.base = repo.commit("add the CRD")
    repo.write(
        {"apps/crd.yaml": crd.replace("    - {name: v1beta1, served: true, storage: false}\n", "")}
    )
    checked = repo.check(capsys)
    assert "drops version v1beta1" in checked.find("GC201", "widgets.example.com")["message"]


def test_a_secret_already_in_git_does_not_block_unrelated_work(repo: Repo, capsys: Cap) -> None:
    repo.write(
        {
            "apps/secret.yaml": (
                "apiVersion: v1\nkind: Secret\nmetadata:\n  name: old\n  namespace: shop\n"
                "stringData:\n  token: committed-long-ago\n"
            )
        }
    )
    repo.edit("apps/kustomization.yaml", "  - config.yaml\n", "  - config.yaml\n  - secret.yaml\n")
    repo.base = repo.commit("an old secret")
    repo.edit("apps/web.yaml", "nginx:1.27.0", "nginx:1.27.1")
    checked = repo.check(capsys)
    assert checked.code == 0
    found = checked.find("GC301", "existing")
    assert found["severity"] == "info" and "apps/secret.yaml (old)" in found["message"]
    assert "committed-long-ago" not in checked.markdown + checked.text
