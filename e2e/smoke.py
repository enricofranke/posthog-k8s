#!/usr/bin/env python3
"""End-to-end smoke test against an installed release.

Port-forwards the router and exercises the paths a real deployment depends on:
  1. ingest listener answers /_health and hides the UI (login page, private API -> 404)
  2. first-user signup through the app listener, session login, project API key
  3. an event sent to /e/ on the ingest listener shows up in the events API
  4. an $exception event becomes an error-tracking issue
  5. a log line sent over OTLP (/i/v1/logs) is accepted
  6. feature flag evaluation through /flags on the ingest listener works

Only the standard library is used so it runs anywhere kubectl runs.
"""
from __future__ import annotations

import argparse
import base64
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


def port_forward(namespace: str, release: str) -> subprocess.Popen:
    svc = f"svc/{release}-router"
    proc = subprocess.Popen(
        ["kubectl", "-n", namespace, "port-forward", svc, "8080:8080", "8081:8081"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    return proc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default="posthog")
    ap.add_argument("--release", default="posthog")
    ap.add_argument("--no-port-forward", action="store_true")
    args = ap.parse_args()

    pf = None if args.no_port_forward else port_forward(args.namespace, args.release)
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

        log("ALL CHECKS PASSED")
        return 0
    finally:
        if pf:
            pf.terminate()


if __name__ == "__main__":
    sys.exit(main())
