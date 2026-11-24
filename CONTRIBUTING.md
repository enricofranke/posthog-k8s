# Contributing

Thanks for helping. This chart is generated, so most changes go through the generator rather
than the templates.

## How the pieces fit

```
tools/rules.yaml          what the generator knows about upstream (ports, probes, secrets, hosts)
tools/sync_upstream.py    reads PostHog's docker-compose at a pinned commit, writes chart data
charts/posthog/upstream.yaml       GENERATED: services, images, env, routes
charts/posthog/files/upstream/     GENERATED: config files mounted as ConfigMaps
charts/posthog/upstream.lock       GENERATED: upstream commit + sha256 of every input
charts/posthog/templates/          generic templates that render upstream.yaml + values.yaml
charts/posthog/ci/kind-values.yaml values used by the end-to-end run in CI
e2e/smoke.py              the end-to-end test
overlay/                  the trusted-header login: a Django module added to PostHog's web image
```

Rule of thumb: if the change is about *what PostHog needs* (a new env var, a new service, a
port), edit `tools/rules.yaml` and run `make sync`. If it is about *how Kubernetes objects look*
(labels, probes shape, a new values option), edit the templates.

## Local setup

```
brew install helm kubeconform kind uv   # or your package manager
helm plugin install https://github.com/helm-unittest/helm-unittest
make test-tools test-overlay lint unittest kubeconform
```

`make test-overlay` runs the login module's unit tests; they cover its decisions and need neither
Django nor PostHog. `make overlay-build` builds the overlay image on the pinned PostHog image
(needs docker); what the module does inside PostHog is covered by the end-to-end run.

`make sync UPSTREAM=<40-char commit>` regenerates the chart data. It needs network access to
GitHub and the container registries (digests) the first time; inputs are cached under `.cache/`.

## Bumping upstream

1. Pick a commit on PostHog `master`: `gh api repos/PostHog/posthog/commits/master --jq .sha`
2. `make sync UPSTREAM=<sha>` – read the diff of `charts/posthog/upstream.yaml` carefully.
3. If the generator fails with "upstream changed in ways the rules do not cover", teach
   `tools/rules.yaml` about the new thing (and add a test in `tools/tests/`).
4. Bump `version` and `appVersion` in `charts/posthog/Chart.yaml`, add a `CHANGELOG.md` entry.
5. Empty `authProxy.image` in `charts/posthog/values.yaml`: the published overlay belongs to the
   previous upstream commit, and `make overlay-check` fails until it is gone or rebuilt. The same
   goes for a PR that changes `overlay/Dockerfile` or the module: the tag carries their hash. If the
   overlay build fails on the new image, upstream moved something the login is anchored to
   (`overlay/Dockerfile` names what).
6. Open a PR. CI runs lint, unit tests and the full end-to-end install on kind.

## Pull requests

- One topic per PR; keep generated files in the same PR as the rule change that produced them.
- Conventional commits (`feat:`, `fix:`, `chore:`, `docs:`) keep the release notes readable.
- CI must be green. The e2e job takes ~20–30 minutes.

## Releasing (maintainers)

The overlay image comes first: build it on the pinned PostHog image, push it as
`posthog-web:$(make overlay-tag)`, sign it, and write `repository:tag@sha256:<digest>` into
`authProxy.image` (`make overlay-check` verifies the tag: upstream commit plus a hash of
`overlay/Dockerfile` and the module). Then:

Tag `vX.Y.Z` matching `Chart.yaml`. The release workflow packages the chart, pushes it to
`oci://ghcr.io/enricofranke/charts/posthog`, signs it with cosign (keyless) and creates a GitHub
release with the `.tgz` attached.
