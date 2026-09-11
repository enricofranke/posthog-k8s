# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository
(Security tab → "Report a vulnerability"). Do not open a public issue for security problems.

You will get an acknowledgement within a few days. Fixes ship as a new chart release with a
changelog entry; credit is given unless you prefer otherwise.

## Scope

This chart packages software made by others. Problems inside PostHog itself belong to
[PostHog's security process](https://github.com/PostHog/posthog/security/policy); problems in
ClickHouse, Redpanda, Temporal, Caddy and so on belong to their projects. What this repository
owns:

- the generated manifests (`charts/posthog/`) and the generator (`tools/`),
- default security settings (security contexts, NetworkPolicies, secret handling, the router's
  ingest allow-list),
- the optional trusted-header authentication overlay.

## Supported versions

Only the latest chart release receives fixes.

## Defaults worth knowing

- Every workload runs non-root with all capabilities dropped and `automountServiceAccountToken: false`.
- The router's `ingest` listener forwards SDK paths only and strips identity headers; the `app`
  listener is meant to sit behind an authenticating proxy or a private ingress.
- Secrets are referenced by name; when the chart generates them it keeps them in one Secret marked
  `helm.sh/resource-policy: keep`. Prefer `existingSecret` / `secrets.keys` in production.
- PostHog's outbound features (webhooks, destinations, data warehouse sources) let authenticated
  users make the server issue HTTP requests. Restrict egress with your CNI if that matters to you;
  the chart's NetworkPolicy option only covers ingress.
