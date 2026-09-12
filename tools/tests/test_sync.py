"""Unit tests for the generator: compose merging, env rewriting, Caddy route extraction and the
guard rails that turn an unknown upstream change into a hard error."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sync_upstream as su  # noqa: E402

RULES = yaml.safe_load((Path(__file__).resolve().parents[1] / "rules.yaml").read_text())


class FakeSource:
    def __init__(self, files: dict[str, str], dirs: dict[str, list[str]] | None = None):
        self.files, self.dirs, self.ref, self.repo, self.hashes = files, dirs or {}, "a" * 40, "x/y", {}

    def file(self, path: str) -> bytes:
        if path not in self.files:
            raise su.SyncError(f"missing {path}")
        return self.files[path].encode()

    def listdir(self, path: str) -> list[str]:
        return self.dirs.get(path, [])


def test_merge_service_overrides_env_and_extends_lists():
    base = {"image": "a", "environment": {"A": "1", "B": "2"}, "volumes": ["x:/x"], "depends_on": ["db"]}
    over = {"environment": ["B=3", "C=4"], "volumes": ["y:/y"], "depends_on": ["kafka"]}
    out = su.merge_service(base, over)
    assert out["environment"] == {"A": "1", "B": "3", "C": "4"}
    assert out["volumes"] == ["x:/x", "y:/y"]
    assert out["depends_on"] == ["db", "kafka"]


def test_bool_env_values_become_lowercase_strings():
    assert su.env_to_dict({"X": True, "Y": False, "Z": 3}) == {"X": "true", "Y": "false", "Z": "3"}


def test_env_rewrites_hosts_secrets_and_variables():
    rw = su.Rewriter(RULES)
    env = rw.env(
        "web",
        {
            "REDIS_URL": "redis://redis7:6379/",
            "KAFKA_HOSTS": "kafka:9092",
            "SECRET_KEY": "$POSTHOG_SECRET",
            "SITE_URL": "https://$DOMAIN",
            "DATABASE_URL": "postgres://posthog:posthog@db:5432/posthog",
            "ENCRYPTION_SALT_KEYS": "00beef0000beef0000beef0000beef00",
            "PLAIN": "value",
            "OPT_OUT_CAPTURE": "${OPT_OUT_CAPTURE:-false}",
        },
    )
    by = {e.name: e for e in env}
    assert by["REDIS_URL"].tpl and 'include "posthog.host" (list $ "redis")' in by["REDIS_URL"].value
    assert by["KAFKA_HOSTS"].value == '{{ include "posthog.host" (list $ "kafka") }}:9092'
    assert by["SECRET_KEY"].secret == "SECRET_KEY"
    assert by["SITE_URL"].secret is None and by["SITE_URL"].tpl
    assert by["DATABASE_URL"].secret == "DATABASE_URL"
    assert by["ENCRYPTION_SALT_KEYS"].secret == "ENCRYPTION_SALT_KEYS"
    assert by["PLAIN"].value == "value" and not by["PLAIN"].tpl
    assert "optOutCapture" in by["OPT_OUT_CAPTURE"].value
    assert rw.problems == []


def test_unknown_variable_and_storage_host_are_problems():
    rw = su.Rewriter(RULES)
    rw.env("svc", {"A": "$NEW_THING", "B": "http://seaweedfs:8333"})
    assert any("unknown compose variable $NEW_THING" in p for p in rw.problems)
    assert any("storage/dropped host 'seaweedfs'" in p for p in rw.problems)


def test_double_dollar_is_a_literal_dollar_not_a_variable():
    rw = su.Rewriter(RULES)
    out, tpl = rw.command("kafka-init", ["-c", "X=$$((ELAPSED + 2)); rpk --brokers kafka:9092"])
    assert out[1].startswith("X=$((ELAPSED + 2))")
    assert 'include "posthog.host" (list $ "kafka")' in out[1]
    assert tpl and rw.problems == []


def test_host_rewrite_does_not_touch_substrings():
    rw = su.Rewriter(RULES)
    text, changed = rw.rewrite_hosts("postgres://x@mydb:5432/webdb kafkaesque", "s", "n")
    assert not changed and text == "postgres://x@mydb:5432/webdb kafkaesque"


def test_parse_caddy_routes_extracts_matchers_targets_and_fallback():
    caddy = """
    @capture {
        path /e
        path /e/*
    }
    @livestream {
        path /livestream/*
    }
    handle @livestream {
        uri strip_prefix /livestream
        reverse_proxy livestream:8080 {
            flush_interval -1
        }
    }
    handle @capture {
        reverse_proxy capture:3000
    }
    handle {
        reverse_proxy web:8000
    }
    """
    routes = su.parse_caddy_routes(caddy)
    names = {r["name"]: r for r in routes}
    assert names["capture"]["paths"] == ["/e", "/e/*"] and names["capture"]["port"] == 3000
    assert names["livestream"]["stripPrefix"] == "/livestream" and names["livestream"]["streaming"]
    assert names["app"]["fallback"] and names["app"]["service"] == "web"


MINI_BASE = """
services:
  worker: &worker
    command: ./bin/docker-worker-celery
    environment: &worker_env
      KAFKA_HOSTS: 'kafka'
  web:
    <<: *worker
    command: ./bin/start-backend
  proxy:
    image: caddy
    environment:
      CADDYFILE: |
        @capture {
            path /e/*
        }
        handle @capture {
            reverse_proxy capture:3000
        }
        handle {
            reverse_proxy web:8000
        }
  capture:
    image: ghcr.io/posthog/posthog/capture:master
    environment:
      ADDRESS: '0.0.0.0:3000'
      KAFKA_HOSTS: 'kafka:9092'
"""
MINI_HOBBY = """
services:
  web:
    extends: {file: docker-compose.base.yml, service: web}
    image: $REGISTRY_URL:$POSTHOG_APP_TAG
    environment:
      SITE_URL: https://$DOMAIN
      SECRET_KEY: $POSTHOG_SECRET
  worker:
    extends: {file: docker-compose.base.yml, service: worker}
    image: $REGISTRY_URL:$POSTHOG_APP_TAG
  proxy:
    extends: {file: docker-compose.base.yml, service: proxy}
  capture:
    extends: {file: docker-compose.base.yml, service: capture}
"""


def mini_rules() -> dict:
    rules = yaml.safe_load(yaml.safe_dump(RULES))
    rules["upstream"]["files"] = []
    rules["upstream"]["dirs"] = []
    rules["mounts"] = {}
    return rules


def test_build_end_to_end_on_a_miniature_compose():
    src = FakeSource({"docker-compose.base.yml": MINI_BASE, "docker-compose.hobby.yml": MINI_HOBBY, ".env.services": "REDIS_URL=redis://redis7:6379/\n"})
    data, files = su.build(mini_rules(), src, want_digests=False)
    assert set(data["services"]) == {"web", "worker", "capture"}
    web = data["services"]["web"]
    assert web["command"] == ["./bin/docker-server"], "hobby wrapper script replaced by the real entrypoint"
    env = {e["name"]: e for e in web["env"]}
    assert env["SECRET_KEY"] == {"name": "SECRET_KEY", "secret": "SECRET_KEY"}
    assert env["IS_BEHIND_PROXY"]["value"] == "true", "env_extra applied"
    assert data["images"]["posthog"]["tag"] == "a" * 40
    assert data["images"]["capture"]["tag"] == "master"
    assert [r["name"] for r in data["routes"]] == ["capture", "app"]
    assert files == {}


def test_build_fails_loudly_on_a_new_upstream_service():
    hobby = MINI_HOBBY + "  brand-new-thing:\n    image: example/new:1\n"
    src = FakeSource({"docker-compose.base.yml": MINI_BASE, "docker-compose.hobby.yml": hobby, ".env.services": ""})
    with pytest.raises(su.SyncError, match="brand-new-thing.*new upstream"):
        su.build(mini_rules(), src, want_digests=False)


def test_rules_cover_every_hobby_service_kind_exactly_once():
    """A service must be either excluded, stateful, a job, or a plain workload with a ports entry."""
    for name in RULES["stateful"]:
        assert name in RULES["ports"], f"stateful {name} needs a ports entry"
    for name in RULES["jobs"]:
        assert name not in RULES["stateful"] and name not in RULES["excluded"]
    for name in RULES["excluded"]:
        assert name not in RULES["ports"], f"excluded {name} must not have ports"


def test_host_rewrite_needs_a_host_context():
    """A compose service name used as a plain value (PLUGIN_SERVER_MODE=recording-api) stays as is;
    the same word is rewritten in URLs, before a port, or as the whole value of a *_HOST variable."""
    rw = su.Rewriter(RULES)
    env = {e.name: e for e in rw.env("recording-api", {
        "PLUGIN_SERVER_MODE": "recording-api",
        "RECORDING_API_URL": "http://recording-api:6738",
        "CYMBAL_REMOTE_RESOLUTION_HOST": "cymbal-resolution",
        "TEMPORAL_HOST": "temporal",
        "KAFKA_HOSTS": "kafka",
        "SOME_LABEL": "kafka",
    })}
    assert env["PLUGIN_SERVER_MODE"].value == "recording-api" and not env["PLUGIN_SERVER_MODE"].tpl
    assert "posthog.serviceName" in env["RECORDING_API_URL"].value
    assert "cymbal-resolution" in env["CYMBAL_REMOTE_RESOLUTION_HOST"].value and env["CYMBAL_REMOTE_RESOLUTION_HOST"].tpl
    assert env["TEMPORAL_HOST"].tpl and env["KAFKA_HOSTS"].tpl
    assert env["SOME_LABEL"].value == "kafka" and not env["SOME_LABEL"].tpl
    assert rw.problems == []


def test_aliases_cover_every_helper_and_service_host():
    src = FakeSource({"docker-compose.base.yml": MINI_BASE, "docker-compose.hobby.yml": MINI_HOBBY, ".env.services": ""})
    data, _ = su.build(mini_rules(), src, want_digests=False)
    assert data["aliases"]["db"] == {"helper": "postgresql"}
    assert data["aliases"]["kafka"] == {"helper": "kafka"}
    assert data["aliases"]["capture"] == {"service": "capture"}
    assert "plugins" not in data["aliases"], "only services that are rendered get an alias"
