# Argo CD

Two Applications work well: one for the cluster-side resources you own (namespace, volumes,
database, secrets), one for the chart with your values file next to them.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: posthog
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "21"
spec:
  project: default
  sources:
    - repoURL: ghcr.io/enricofranke/charts
      chart: posthog
      targetRevision: 0.1.1
      helm:
        releaseName: posthog
        valueFiles:
          - $values/platform/posthog/values.yaml
    - repoURL: https://git.example.com/infra/cluster.git
      targetRevision: main
      ref: values
  destination:
    server: https://kubernetes.default.svc
    namespace: posthog
  syncPolicy:
    automated: {prune: false, selfHeal: true}
    syncOptions:
      - ServerSideApply=true
```

For OCI charts add `ghcr.io/enricofranke/charts` to the AppProject's `sourceRepos` and, on Argo CD
versions that need it, a repository Secret with `enableOCI: "true"`. To follow the main branch
during evaluation use the git source instead: `repoURL: https://github.com/enricofranke/posthog-k8s.git`,
`path: charts/posthog`, `targetRevision: main`.

Things to know:

- **Secrets:** Argo renders with `helm template`, so `lookup` does not run and a generated secret
  would be regenerated on every sync. Set all secrets via `existingSecret` / `secrets.keys` (see
  [production.md](production.md#5-secrets)); the chart's own Secret then only contains
  deterministic values.
- **Hooks:** `kafka-init` runs as a sidecar, `migrate` is a regular Job with a version-specific
  name (Jobs are immutable) and `argocd.argoproj.io/sync-options: Replace=true`,
  `asyncmigrationscheck` is a `post-install,post-upgrade` hook that Argo maps to `PostSync`.
- **Server-side apply** keeps the large ClickHouse ConfigMaps under the annotation size limit.
- **Health:** `web` and the workers wait for the migrate Job in an init container; the
  Application shows `Progressing` until migrations are through (10–25 minutes on a fresh
  database without a [schema seed](../README.md#fast-first-start-schema-seed)).
