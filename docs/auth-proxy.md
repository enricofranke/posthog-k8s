# Trusted-header login

Status: **implemented, not released yet**. The chart side and the login module (`overlay/`) are in
the repository; the overlay image is published and pinned in `authProxy.image` by the release
process. While `authProxy.image` is empty, `authProxy.enabled: true` only renders with an image
you built yourself (`make overlay-build`).

## Goal

Users who reach the `app` listener through an authenticating proxy (Teleport Application Access,
oauth2-proxy, Pomerium, Authelia, Cloudflare Access) should be logged into PostHog automatically,
and their organization memberships should follow the groups the proxy asserts:

```
X-Forwarded-Email:  jane@example.com
X-Forwarded-User:   jane
X-Forwarded-Groups: platform-admins,acme-developers
```

```yaml
authProxy:
  enabled: true
  groups:
    platform-admins: {organizations: ["*"], level: admin}
    acme-developers: {organizations: [acme], level: member}

networkPolicy:
  enabled: true
  appIngressFrom:
    - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: oauth2-proxy}}
```

- Accounts are matched by email (`authProxy.emailHeader`). **The proxy must only assert verified
  addresses**: whoever can make it send `jane@example.com` is Jane. A new account is created
  without a password and with the email marked as verified.
- `authProxy.userHeader` is never used to identify anyone. It is the display name of an account at
  the moment the login creates it, nothing else; a proxy that does not set it leaves the name to
  the part of the email before the `@`.
- `platform-admins` → admin in every organization; `acme-developers` → member of the organization
  with slug `acme`. Organizations are referenced by slug or ID (names are not unique) and must
  exist; the login never creates one, and a reference that matches nothing is logged and skipped.
  Where several groups name the same organization, admin wins.
- Groups are authoritative for member and admin memberships: a membership without a group is
  removed, a level that differs is set. Owner is never granted, lowered or removed.
- A user who ends up without any organization gets 403. A new user is then not created; an
  existing one is deactivated (`is_active=False`, which also ends their sessions) and reactivated
  when a group grants access again. An owner keeps access to what they own whatever their groups.
  `is_active` follows the groups in both directions: an account you deactivated by hand comes back
  with the next login as long as a group maps it. Take the group away instead.
- Reconciliation runs when the session's (email, groups) or the `authProxy.groups` mapping
  changes, not on every request. So a user removed from the identity provider is only deactivated
  if they show up again, and an organization created later is joined through `"*"` at the next
  login.
- PostHog treats these sessions like its other single sign-on logins: it does not ask for its own
  two-factor code. A second factor is the proxy's job.

### Password login and break-glass

With the default `authProxy.passwordLogin: true`, PostHog's own ways in keep working next to the
proxy: on a request without an email header (a route your proxy lets through unauthenticated, an
API client with a personal API key) nothing changes. `passwordLogin: false` makes the proxy the
only way in. `web` then answers 403 on password and passkey login, signup, invite acceptance,
password reset, email verification and the social and partner logins. Personal API keys keep
working; they are not a login.

Nobody gets in while the proxy or the identity provider is down. To get in anyway:

1. Keep one owner account with a password. The account that signed up first owns its organization,
   and the group mapping never touches an owner.
2. Set `authProxy.passwordLogin: true` and let `web` roll.
3. Reach the app listener without the proxy, for example with
   `kubectl -n <namespace> port-forward svc/<release>-router 8080:8080` and
   `http://localhost:8080`. Port forwarding is not subject to the NetworkPolicy.
4. Set `passwordLogin` back.

## The overlay image

PostHog's self-hosted edition has no trusted-header or OIDC login backend; SAML is a paid,
Cloud-only feature. The login is a small Django module (`overlay/posthog_k8s_authproxy`): a
middleware right after PostHog's `AuthenticationMiddleware` and an authentication backend that
names its sessions. It is delivered as an overlay image
`ghcr.io/enricofranke/posthog-k8s/posthog-web:<upstream-sha>-<source-hash>`, built `FROM posthog/posthog@<digest>`
and pinned by digest like the other images. Only `web` runs it, and only with
`authProxy.enabled`. The tag names both inputs: the PostHog commit and a hash of
`overlay/Dockerfile` and the module (`make overlay-tag`). CI fails when `authProxy.image` carries
another tag, so a pinned image cannot outlive an upstream bump or a change to the login.

The image adds the module and appends one hook to `posthog/settings/__init__.py`, which does
nothing unless `AUTH_PROXY_ENABLED` is set. Nothing of PostHog is replaced, and nothing under
`ee/` is used or changed. A mounted module instead of an image does not work without replacing
upstream's ASGI entry point, which sets `DJANGO_SETTINGS_MODULE` unconditionally.

The image follows the chart's `appVersion`. If you override the PostHog image
(`images.overrides.posthog`), build the overlay on that image and set `authProxy.image`:

```
docker build --build-arg POSTHOG_IMAGE=<repository>@sha256:<digest> -t <registry>/posthog-web:<tag> overlay
```

The build refuses a base that is not pinned by digest; `--build-arg ALLOW_UNPINNED_BASE=true`
overrides that.

`web` does not start when the login is enabled and its configuration is broken (empty secret,
unknown level, a PostHog without the middleware the login is anchored to), so a rollout stops
instead of serving without the login.

## Trust model

`web` cannot tell from the identity headers alone where a request came from: every pod of the
release may talk to `web:8000` directly, and some of them fetch pages on behalf of users. Three
things together make the headers trustworthy:

1. **Only the proxy reaches the `app` listener.** With `networkPolicy.enabled` the router is not
   part of the policy that opens the release's pods to each other; its `app` port accepts
   `networkPolicy.appIngressFrom` and nobody else, the release's own pods included. The chart
   refuses to render `authProxy.enabled: true` without `networkPolicy.enabled: true` and a
   non-empty `appIngressFrom`.
2. **Only the `app` listener knows the shared secret.** The router adds
   `X-Posthog-K8s-Auth-Proxy: <AUTH_PROXY_SECRET>` to what the `app` listener forwards to `web`,
   replacing any copy the client sent. The login backend honours the identity headers only on
   requests that carry it; a pod that goes around the router does not have the secret.
3. **The `ingest` listener strips** `authProxy.userHeader`, `emailHeader`, `groupsHeader`, the
   secret header and `router.stripHeaders`, whether `authProxy` is enabled or not.

The login also answers 400 to a request in which an identity header or the secret header occurs
more than once. Django joins repeated headers with a comma, so a proxy that appends its header to
one the client sent, instead of replacing it, would let the client add groups. The login therefore
reads the raw header list of the request, not Django's merged view, and matches exact names.

Spellings count as repeats: Django reads `X_Forwarded_Groups` and `X-Forwarded-Groups` as the same
header, while Caddy and most proxies forward the underscore form untouched next to the one they
set. A request that carries an identity header or the secret header under any other spelling is
refused with 400, whether or not the exact header is there too. An email header that is not
exactly one address is refused the same way.

Once checked, the secret header is taken off the request, so views, logs and error reports of
PostHog do not see it.

This mirrors how Argo CD (Dex authproxy connector) and Headlamp trust an upstream proxy. Your part
is the proxy: it has to set all three headers on every request, overwriting what the client sent.

### The shared secret

`AUTH_PROXY_SECRET` (at least 32 characters, printable ASCII without spaces or line breaks) is
handled like the chart's other secrets: `authProxy.sharedSecret` inline,
generated and kept across upgrades when empty, read from `posthog.existingSecret` when that is set,
or mapped with `secrets.keys.AUTH_PROXY_SECRET: {secret: <name>, key: <key>}`. Under Argo CD use
one of the last two; a generated value would change on every sync. Caddy gets it through its
environment, not through the ConfigMap, and neither the router nor `web` starts with an empty one.
The pods read it at start: restart the router and `web` after rotating it.

## Not in scope

Project-level access control inside an organization is a paid PostHog feature. Organizations are
the isolation boundary on self-hosted instances; the mapping above is deliberately organization
based.
