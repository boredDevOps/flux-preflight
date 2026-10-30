# Third-party software

The source in this repository is under Apache-2.0 (see `LICENSE` and `NOTICE`). The Docker image
also ships the programs below, unmodified, each under its own license. Their versions are pinned
in the `Dockerfile` and `pyproject.toml`.

| Component | License | Source | How it is used |
|---|---|---|---|
| Flux CLI | Apache-2.0 | https://github.com/fluxcd/flux2 | separate program, run by `flux-preflight` |
| kubeconform | Apache-2.0 | https://github.com/yannh/kubeconform | separate program, run by `flux-preflight` |
| yamllint | GPL-3.0-or-later | https://github.com/adrienverge/yamllint | separate program, run by `flux-preflight` |
| PyYAML | MIT | https://github.com/yaml/pyyaml | Python library |
| pathspec | MPL-2.0 | https://github.com/cpburnz/python-pathspec | Python library, used by yamllint |

The license text of each Python package is in the image under `site-packages/*.dist-info/`. The
Apache-2.0 text that covers Flux and kubeconform is `/opt/flux-preflight/LICENSE`. The base image
(`python:3.12-slim-bookworm`) and `git` come with their own licenses under `/usr/share/doc/`.
