#!/usr/bin/env python3
"""End-to-end smoke test against an installed release.

Port-forwards the router and exercises the paths a real deployment depends on:
  1. ingest listener answers /_health and hides the UI (login page, private API -> 404)
  2. first-user signup through the app listener, session login, project API key
  3. an event sent to /e/ on the ingest listener shows up in the events API
  4. an $exception event becomes an error-tracking issue
  5. a log line sent over OTLP (/i/v1/logs) is accepted
  6. feature flag evaluation through /flags on the ingest listener works
  7. with --auth-proxy (the release runs with authProxy.enabled and the groups of
     charts/posthog/ci/kind-values.yaml): identity headers on the app listener log a user in and
     drive the memberships; the same headers on the ingest listener or straight to web do nothing

Only the standard library is used so it runs anywhere kubectl runs.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import http.cookiejar
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

APP = "http://localhost:8080"
INGEST = "http://localhost:8081"
WEB = "http://localhost:8000"

MEMBER, ADMIN, OWNER = 1, 8, 15


def log(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def wait_for(fn, what: str, timeout: int = 600, interval: int = 5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            result = fn()
            if result:
                return result
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(interval)
    raise SystemExit(f"timed out waiting for {what} (last error: {last})")


class Client:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf: str | None = None

    def request(self, method: str, url: str, body=None, headers=None, timeout=60) -> tuple[int, dict, bytes]:
        data = None
        headers = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        if self.csrf and method not in ("GET", "HEAD"):
            headers.setdefault("X-CSRFToken", self.csrf)
            headers.setdefault("Referer", url)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
        finally:
            for c in self.jar:
                if c.name == "posthog_csrftoken":
                    self.csrf = c.value

    def json(self, method: str, url: str, body=None, headers=None, expect=(200, 201)):
        status, _, raw = self.request(method, url, body, headers)
        if status not in expect:
            raise RuntimeError(f"{method} {url} -> {status}: {raw[:300]!r}")
        return json.loads(raw) if raw else None


def port_forward(namespace: str, service: str, *ports: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        ["kubectl", "-n", namespace, "port-forward", f"svc/{service}", *ports],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    return proc


def identity(email: str, *groups: str) -> dict[str, str]:
    """What an authenticating proxy in front of the app listener would send."""
    return {
        "X-Forwarded-Email": email,
        "X-Forwarded-User": email.split("@")[0],
        "X-Forwarded-Groups": ",".join(groups),
    }


def raw_status(base: str, path: str, headers: list[tuple[str, str]]) -> int:
    """GET with the headers exactly as listed; urllib cannot send one header twice."""
    url = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=60)
    try:
        conn.putrequest("GET", path)
        for name, value in headers:
            conn.putheader(name, value)
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def check_auth_proxy(owner: Client, owner_email: str, namespace: str, release: str) -> None:
    run = uuid.uuid4().hex[:8]

    def me(client: Client, email: str, *groups: str, base: str = APP) -> tuple[int, dict]:
        status, _, raw = client.request("GET", f"{base}/api/users/@me/", headers=identity(email, *groups))
        return status, (json.loads(raw) if status == 200 else {})

    def members() -> dict[str, int]:
        data = owner.json("GET", f"{APP}/api/organizations/@current/members/?limit=500")
        return {m["user"]["email"]: m["level"] for m in data["results"]}

    def is_active(email: str) -> str:
        out = subprocess.run(
            ["kubectl", "-n", namespace, "exec", f"{release}-postgresql-0", "--", "psql", "-U", "posthog", "-d",
             "posthog", "-tAc", f"SELECT is_active FROM posthog_user WHERE email = '{email}'"],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        return out.stdout.strip()

    # no mapped group, or only organizations that do not exist: 403 and no account
    for label, group in (("no mapped group", "everyone"), ("unknown organization", "e2e-nowhere")):
        email = f"{group}-{run}@example.com"
        status, _ = me(Client(), email, group)
        assert status == 403, f"{label}: expected 403, got {status}"
        assert email not in members(), f"{label}: the user was created"
        assert is_active(email) == "", f"{label}: an account exists"
    log("auth proxy: users without a mapped group get 403 and no account")

    # first login creates the user with the mapped membership; the session then stands on its own
    member_email = f"member-{run}@example.com"
    member = Client()
    status, body = me(member, member_email, "e2e-members")
    assert status == 200 and body["email"] == member_email, f"member login: {status} {body.get('email')}"
    member_uuid = body["uuid"]
    assert members().get(member_email) == MEMBER, f"member level: {members().get(member_email)}"
    assert member.request("GET", f"{APP}/api/users/@me/")[0] == 200, "the proxy session does not hold without headers"
    log(f"auth proxy: {member_email} was created as a member and holds a session")

    # the level follows the groups in both directions
    status, _ = me(member, member_email, "e2e-members", "e2e-admins")
    assert status == 200 and members().get(member_email) == ADMIN, "admin group did not raise the level"
    status, _ = me(member, member_email, "e2e-members")
    assert status == 200 and members().get(member_email) == MEMBER, "losing the admin group did not lower the level"
    log("auth proxy: the membership level follows the groups up and down")

    # an identity header that occurs twice is refused, also for a user who is logged in
    for name, values in (
        ("X-Forwarded-Groups", ("e2e-members", "e2e-admins")),
        ("X-Forwarded-Email", (member_email, f"other-{run}@example.com")),
    ):
        headers = [(k, v) for k, v in identity(member_email, "e2e-members").items() if k != name]
        headers += [(name, v) for v in values]
        status = raw_status(APP, "/api/users/@me/", headers)
        assert status == 400, f"repeated {name}: expected 400, got {status}"
    assert members().get(member_email) == MEMBER, "a repeated groups header changed the level"
    log("auth proxy: a repeated identity header is refused with 400")

    # so is another spelling of one: Django reads X_Forwarded_Groups as X-Forwarded-Groups
    proxy_headers = list(identity(member_email, "e2e-members").items())
    for extra in (
        ("X_Forwarded_Groups", "e2e-admins"),
        ("X_Forwarded_Email", f"other-{run}@example.com"),
        ("X_Posthog_K8s_Auth_Proxy", "anything"),
    ):
        status = raw_status(APP, "/api/users/@me/", [*proxy_headers, extra])
        assert status == 400, f"{extra[0]} next to the proxy's headers: expected 400, got {status}"
    without_groups = [(k, v) for k, v in proxy_headers if k != "X-Forwarded-Groups"]
    status = raw_status(APP, "/api/users/@me/", [*without_groups, ("X_Forwarded_Groups", "e2e-admins")])
    assert status == 400, f"X_Forwarded_Groups in place of the groups header: expected 400, got {status}"
    assert members().get(member_email) == MEMBER, "an underscore spelling of the groups header changed the level"
    log("auth proxy: an identity or secret header under another spelling is refused with 400")

    # the ingest listener strips the headers. /robots.txt is a web path both listeners forward and
    # one the login sees: the same request on the app listener does create the user.
    ingest_email, control_email = f"ingest-{run}@example.com", f"control-{run}@example.com"
    status = Client().request("GET", f"{INGEST}/robots.txt", headers=identity(ingest_email, "e2e-admins"))[0]
    assert status == 200, f"ingest /robots.txt: {status}"
    status = Client().request("GET", f"{APP}/robots.txt", headers=identity(control_email, "e2e-admins"))[0]
    assert status == 200, f"app /robots.txt: {status}"
    seen = members()
    assert seen.get(control_email) == ADMIN, "control: identity headers on the app listener did not create the user"
    assert ingest_email not in seen, "identity headers on the ingest listener created a user"
    log("auth proxy: identity headers on the ingest listener are ignored")

    # straight to web, around the router: no shared secret, no login
    direct_email = f"direct-{run}@example.com"
    for extra in ({}, {"X-Posthog-K8s-Auth-Proxy": "guess"}):
        client = Client()
        headers = {**identity(direct_email, "e2e-admins"), **extra}
        status = client.request("GET", f"{WEB}/api/users/@me/", headers=headers)[0]
        assert status == 401, f"direct to web: expected 401, got {status}"
    assert direct_email not in members(), "identity headers sent straight to web created a user"
    log("auth proxy: identity headers sent straight to web are ignored")

    # no group left: 403, membership gone, account deactivated, session over; back with the group
    cookies = [("Cookie", "; ".join(f"{c.name}={c.value}" for c in member.jar))]
    assert raw_status(WEB, "/api/users/@me/", cookies) == 200, "the session cookie alone does not authenticate"
    status, _ = me(member, member_email, "everyone")
    assert status == 403, f"member without groups: expected 403, got {status}"
    assert member_email not in members(), "the membership outlived the group"
    assert is_active(member_email) == "f", f"the user was not deactivated: {is_active(member_email)!r}"
    assert raw_status(WEB, "/api/users/@me/", cookies) == 401, "the session outlived the deactivation"
    status, body = me(member, member_email, "e2e-members")
    assert status == 200 and body["uuid"] == member_uuid, f"return of the member: {status}"
    assert is_active(member_email) == "t" and members().get(member_email) == MEMBER, "the user was not reactivated"
    log("auth proxy: a user without groups is deactivated, and reactivated when a group returns")

    # owner is never lowered or removed, and keeps access without any group
    for groups in (("e2e-members",), ("everyone",)):
        status, body = me(Client(), owner_email, *groups)
        assert status == 200 and body["email"] == owner_email, f"owner with {groups}: {status}"
        assert members().get(owner_email) == OWNER, f"owner level changed with {groups}"
    log("auth proxy: the owner stays owner whatever the groups say")

    # organizations by slug
    slug = owner.json("GET", f"{APP}/api/organizations/@current/")["slug"]
    if slug == "smoke-org":
        slug_email = f"slug-{run}@example.com"
        status, _ = me(Client(), slug_email, "e2e-smoke-org")
        assert status == 200 and members().get(slug_email) == MEMBER, f"mapping by slug: {status}"
        log("auth proxy: a group mapped to an organization by slug grants the membership")
    else:
        log(f"auth proxy: organization slug is {slug!r}, not smoke-org (not a fresh install); slug mapping not checked")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default="posthog")
    ap.add_argument("--release", default="posthog")
    ap.add_argument("--no-port-forward", action="store_true")
    ap.add_argument("--auth-proxy", action="store_true", help="also check the trusted-header login")
    args = ap.parse_args()

    forwards = []
    if not args.no_port_forward:
        forwards.append(port_forward(args.namespace, f"{args.release}-router", "8080:8080", "8081:8081"))
        if args.auth_proxy:
            forwards.append(port_forward(args.namespace, f"{args.release}-web", "8000:8000"))
    c = Client()
    try:
        # 1. ingest listener
        wait_for(lambda: c.request("GET", f"{INGEST}/_health")[0] == 200, "router /_health", timeout=300)
        log("ingest /_health ok")
        for path in ("/login", "/api/projects/", "/api/users/@me/"):
            status = c.request("GET", f"{INGEST}{path}")[0]
            assert status == 404, f"ingest listener exposed {path} ({status})"
        log("ingest listener hides the UI and private API")
        wait_for(lambda: c.request("GET", f"{APP}/_health")[0] == 200, "web /_health", timeout=900)

        # 2. first user
        email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
        password = "Smoke-" + uuid.uuid4().hex
        c.request("GET", f"{APP}/login")  # csrf cookie
        signup = {
            "first_name": "Smoke",
            "email": email,
            "password": password,
            "organization_name": "Smoke Org",
            "role_at_organization": "engineering",
        }
        status, _, raw = c.request("POST", f"{APP}/api/signup/", signup)
        if status not in (200, 201):
            raise SystemExit(f"signup failed: {status} {raw[:500]!r}")
        log(f"signed up {email}")
        me = c.json("GET", f"{APP}/api/users/@me/")
        team = me["team"]
        token = team["api_token"]
        project_id = team["id"]
        log(f"project {project_id}, token {token[:8]}...")

        # 3. event
        distinct = f"user-{uuid.uuid4().hex[:6]}"
        event_name = f"smoke_event_{uuid.uuid4().hex[:6]}"
        payload = {"api_key": token, "event": event_name, "distinct_id": distinct, "properties": {"$lib": "smoke"}}
        status, _, raw = c.request("POST", f"{INGEST}/e/", payload)
        assert status == 200, f"capture returned {status}: {raw[:200]!r}"
        log("event accepted by capture")

        def event_visible():
            data = c.json("GET", f"{APP}/api/projects/{project_id}/events/?event={urllib.parse.quote(event_name)}")
            return any(e["event"] == event_name for e in data.get("results", []))

        wait_for(event_visible, "event in events API", timeout=600, interval=10)
        log("event visible in the events API (ingestion -> ClickHouse works)")

        # 4. exception -> issue
        exc = {
            "api_key": token,
            "event": "$exception",
            "distinct_id": distinct,
            "properties": {
                "$exception_list": [
                    {"type": "SmokeError", "value": f"smoke {uuid.uuid4().hex[:6]}", "mechanism": {"handled": True}}
                ],
                "$lib": "smoke",
            },
        }
        status, _, raw = c.request("POST", f"{INGEST}/e/", exc)
        assert status == 200, f"exception capture returned {status}"

        def issue_visible():
            data = c.json("GET", f"{APP}/api/projects/{project_id}/error_tracking/issues/?limit=5")
            return any("SmokeError" in json.dumps(i) for i in data.get("results", []))

        wait_for(issue_visible, "error tracking issue", timeout=600, interval=10)
        log("exception became an error-tracking issue (cymbal pipeline works)")

        # 5. logs over OTLP
        now_ns = int(time.time() * 1e9)
        otlp = {
            "resourceLogs": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "smoke"}}]},
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "timeUnixNano": str(now_ns),
                                    "severityText": "INFO",
                                    "body": {"stringValue": f"smoke log {uuid.uuid4().hex[:6]}"},
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        status, _, raw = c.request("POST", f"{INGEST}/i/v1/logs", otlp, headers={"Authorization": f"Bearer {token}"})
        assert status in (200, 202), f"OTLP logs returned {status}: {raw[:200]!r}"
        log("OTLP log accepted by capture-logs")

        # 6. flags
        status, _, raw = c.request("POST", f"{INGEST}/flags/?v=2", {"api_key": token, "distinct_id": distinct})
        assert status == 200, f"/flags returned {status}: {raw[:200]!r}"
        log("feature flag evaluation works")

        # 7. trusted-header login
        if args.auth_proxy:
            check_auth_proxy(c, email, args.namespace, args.release)

        log("ALL CHECKS PASSED")
        return 0
    finally:
        for forward in forwards:
            forward.terminate()


if __name__ == "__main__":
    sys.exit(main())
