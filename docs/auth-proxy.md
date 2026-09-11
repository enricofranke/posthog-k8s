# Trusted-header login (roadmap)

Status: **planned, not implemented**. The values under `authProxy.*` are reserved and currently
have no effect.

## Goal

Users who reach the `app` listener through an authenticating proxy (Teleport Application Access,
oauth2-proxy, Pomerium, Authelia, Cloudflare Access) should be logged into PostHog automatically,
and their organization memberships should follow the groups the proxy asserts:

```
X-Forwarded-User:   jane@example.com
X-Forwarded-Groups: netcup-admins,zundertrack-developers
```

- unknown user → created on first request
- `netcup-admins` → admin in every organization
- `zundertrack-developers` → member of organization "Zundertrack"
- group disappears → membership removed on next request (offboarding follows the proxy)

## Why an overlay image

PostHog's self-hosted edition has no trusted-header or OIDC login backend; SAML is a paid,
Cloud-only feature. Django itself ships `RemoteUserMiddleware`/`RemoteUserBackend`, so the plan
is a ~150-line module plus a settings patch, delivered as an overlay image
`ghcr.io/enricofranke/posthog-k8s/posthog-web:<upstream-sha>` built `FROM posthog/posthog@<digest>`.
Only the `web` service uses it.

## Trust model

The headers are only honoured on requests that arrive at the router's `app` listener. That
listener must be reachable solely from the proxy (NetworkPolicy `appIngressFrom`). The `ingest`
listener strips the headers unconditionally. This mirrors how Argo CD (Dex authproxy connector)
and Headlamp trust an upstream proxy.

## Not in scope

Project-level access control inside an organization is a paid PostHog feature. Organizations are
the isolation boundary on self-hosted instances; the mapping above is deliberately organization
based.
