#!/usr/bin/env python3
"""Turn PostHog's hobby docker-compose into chart data.

Reads docker-compose.base.yml + docker-compose.hobby.yml at a pinned upstream commit, applies
tools/rules.yaml and writes:

  charts/posthog/upstream.yaml          services, images, env, routes (consumed by the templates)
  charts/posthog/files/upstream/...     config files mounted as ConfigMaps
  charts/posthog/upstream.lock          sha256 of every upstream input, for reproducibility

Usage:
  sync_upstream.py --ref <commit-sha> [--cache DIR] [--offline] [--no-digests]

Every compose construct the generator does not understand is a hard error, so an upstream change
surfaces as a failing sync instead of a silently wrong chart.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = Path(__file__).resolve().parent / "rules.yaml"
CHART_DIR = ROOT / "charts" / "posthog"
FILES_DIR = CHART_DIR / "files" / "upstream"
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
API = "https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"

VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}|\$([A-Z][A-Z0-9_]*)")
PG_URL_RE = re.compile(r"^postgres(?:ql)?://[^@\s]+@([a-z0-9-]+)(?::\d+)?/([a-zA-Z0-9_]+)$")


class SyncError(Exception):
    pass


# ----------------------------------------------------------------------------- fetching


class Source:
    """Fetches upstream files, caching them on disk so a sync is reproducible offline."""

    def __init__(self, repo: str, ref: str, cache: Path, offline: bool):
        self.repo, self.ref, self.cache, self.offline = repo, ref, cache, offline
        self.hashes: dict[str, str] = {}

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "posthog-k8s-sync"})
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed https hosts
            return r.read()

    def file(self, path: str) -> bytes:
        local = self.cache / self.ref / path
        if local.exists():
            data = local.read_bytes()
        elif self.offline:
            raise SyncError(f"offline and {path} not cached under {local}")
        else:
            try:
                data = self._get(RAW.format(repo=self.repo, ref=self.ref, path=path))
            except urllib.error.HTTPError as e:
                raise SyncError(f"cannot fetch {path}@{self.ref}: {e}") from e
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
        self.hashes[path] = hashlib.sha256(data).hexdigest()
        return data

    def listdir(self, path: str) -> list[str]:
        idx = self.cache / self.ref / path / ".index.json"
        if idx.exists():
            return json.loads(idx.read_text())
        if self.offline:
            raise SyncError(f"offline and directory {path} not cached")
        data = json.loads(self._get(API.format(repo=self.repo, ref=self.ref, path=path)))
        names = [e["path"] for e in data if e["type"] == "file"]
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text(json.dumps(names))
        return names


# ----------------------------------------------------------------------------- compose


def load_compose(source: Source, names: list[str]) -> dict[str, dict]:
    """Resolve compose `extends` across files and return the effective hobby services."""
    docs = {n: yaml.safe_load(source.file(n)) for n in names}
    top = names[-1]
    resolved: dict[str, dict] = {}

    def resolve(file: str, name: str, seen: tuple = ()) -> dict:
        if (file, name) in seen:
            raise SyncError(f"extends cycle at {file}:{name}")
        svc = copy.deepcopy(docs[file]["services"].get(name))
        if svc is None:
            raise SyncError(f"{file} has no service {name}")
        ext = svc.pop("extends", None)
        if not ext:
            return svc
        base = resolve(ext.get("file", file), ext["service"], seen + ((file, name),))
        return merge_service(base, svc)

    for name in docs[top]["services"]:
        resolved[name] = resolve(top, name)
    return resolved


def _env_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def env_to_dict(env: Any) -> dict[str, str]:
    if env is None:
        return {}
    if isinstance(env, dict):
        return {k: _env_str(v) for k, v in env.items()}
    out = {}
    for item in env:
        k, _, v = str(item).partition("=")
        out[k] = v
    return out


def merge_service(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k == "environment":
            out[k] = {**env_to_dict(out.get(k)), **env_to_dict(v)}
        elif k in ("volumes", "depends_on", "networks", "env_file") and isinstance(v, list):
            existing = out.get(k) or []
            if isinstance(existing, dict):
                existing = list(existing)
            out[k] = existing + [x for x in v if x not in existing]
        elif k == "depends_on" and isinstance(v, dict):
            existing = out.get(k) or {}
            if isinstance(existing, list):
                existing = {x: {"condition": "service_started"} for x in existing}
            out[k] = {**existing, **v}
        else:
            out[k] = v
    if isinstance(out.get("environment"), list):
        out["environment"] = env_to_dict(out["environment"])
    return out


def split_command(cmd: Any) -> list[str] | None:
    if cmd is None:
        return None
    if isinstance(cmd, list):
        return [str(c) for c in cmd]
    import shlex

    return shlex.split(str(cmd))


# ----------------------------------------------------------------------------- env rewriting


@dataclass
class Env:
    name: str
    value: str | None = None
    secret: str | None = None
    tpl: bool = False
    optional: bool = False

    def out(self) -> dict:
        d: dict[str, Any] = {"name": self.name}
        if self.secret:
            d["secret"] = self.secret
            if self.optional:
                d["optional"] = True
        else:
            d["value"] = self.value
            if self.tpl:
                d["tpl"] = True
        return d


@dataclass
class Rewriter:
    rules: dict
    images: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def host_expr(self, host: str) -> str | None:
        rule = self.rules["hosts"].get(host)
        if rule is None:
            return None
        if "helper" in rule:
            return '{{ include "posthog.host" (list $ "%s") }}' % rule["helper"]
        if "service" in rule:
            return '{{ include "posthog.serviceName" (list $ "%s") }}' % rule["service"]
        return None

    def substitute_vars(self, text: str, service: str, name: str) -> tuple[str, Env | None]:
        """Replace compose ${VAR} references. Returns (text, env) where env is set when the whole
        value is a secret reference."""
        whole_secret: Env | None = None

        def repl(m: re.Match) -> str:
            nonlocal whole_secret
            var = m.group(1) or m.group(3)
            default = m.group(2)
            rule = self.rules["variables"].get(var)
            if rule is None:
                self.problems.append(f"{service}.{name}: unknown compose variable ${var}")
                return m.group(0)
            if "secret" in rule:
                if m.group(0) == escaped.strip("'\""):
                    whole_secret = Env(name, secret=rule["secret"], optional=rule.get("optional", False))
                    return ""
                self.problems.append(f"{service}.{name}: secret ${var} embedded in a larger value")
                return m.group(0)
            if "tpl" in rule:
                return rule["tpl"]
            if "literal" in rule:
                return rule["literal"]
            if "image" in rule:
                return "__IMAGE__"
            self.problems.append(f"{service}.{name}: rule for ${var} has no action")
            return m.group(0)

        # Compose escapes a literal dollar as `$$` (shell variables inside scripts).
        escaped = text.replace("$$", "\x00")
        out = VAR_RE.sub(repl, escaped).replace("\x00", "$")
        if whole_secret:
            return "", whole_secret
        return out, None

    HOST_ENV_SUFFIXES = ("_HOST", "_HOSTS", "_SEEDS", "_ADDR", "_ADDRESS")

    def rewrite_hosts(self, text: str, service: str, name: str) -> tuple[str, bool]:
        """Rewrite compose hostnames to chart services.

        A hostname is only rewritten in a context where it is unambiguously a host: after `//` or
        `@` (URLs), before `:<port>`, or as the whole value of a variable whose name says it is a
        host (`*_HOST`, `*_HOSTS`, `*_SEEDS`, `*_ADDR`). That keeps values like
        PLUGIN_SERVER_MODE=recording-api intact."""
        changed = False
        hosts = self.rules["hosts"]
        alt = "|".join(re.escape(h) for h in hosts)
        pattern = re.compile(r"(?P<pre>^|[\s'\"=,]|//|@)(?P<host>" + alt + r")(?P<post>$|[:/\s'\",])")
        host_var = name.upper().endswith(self.HOST_ENV_SUFFIXES)

        def repl(m: re.Match) -> str:
            nonlocal changed
            host, pre, post = m.group("host"), m.group("pre"), m.group("post")
            after = text[m.end("host"):m.end("host") + 2]
            in_url = pre in ("//", "@")
            has_port = post == ":" and after[1:2].isdigit()
            whole_value = m.start("host") == 0 and m.end("host") == len(text)
            if not (in_url or has_port or (whole_value and host_var)):
                return m.group(0)
            rule = hosts[host]
            if rule.get("storage") or rule.get("drop"):
                self.problems.append(f"{service}.{name}: storage/dropped host {host!r} used in {text!r}")
                return m.group(0)
            changed = True
            return f"{pre}{self.host_expr(host)}{post}"

        return pattern.sub(repl, text), changed

    def env(self, service: str, raw: dict[str, str]) -> list[Env]:
        out: list[Env] = []
        for name, value in raw.items():
            value = "" if value is None else str(value)
            lit = self.rules["env_literals"].get(name, {}).get(value)
            if lit:
                out.append(Env(name, secret=lit["secret"]))
                continue
            whole = self.rules["env_values"].get(name)
            if whole:
                if "secret" in whole:
                    out.append(Env(name, secret=whole["secret"]))
                else:
                    out.append(Env(name, value=whole["tpl"], tpl=True))
                continue
            if PG_URL_RE.match(value):
                self.problems.append(f"{service}.{name}: postgres URL not covered by env_values")
            text, whole_secret = self.substitute_vars(value, service, name)
            if whole_secret:
                out.append(whole_secret)
                continue
            tpl = "{{" in text
            text, changed = self.rewrite_hosts(text, service, name)
            out.append(Env(name, value=text, tpl=tpl or changed))
        return out

    def command(self, service: str, cmd: list[str] | None) -> tuple[list[str] | None, bool]:
        if not cmd:
            return cmd, False
        out, tpl = [], False
        for part in cmd:
            text, _ = self.substitute_vars(part, service, "command")
            text, changed = self.rewrite_hosts(text, service, "command")
            tpl = tpl or changed or "{{" in text
            out.append(text)
        return out, tpl


# ----------------------------------------------------------------------------- images


def image_key(rules: dict, service: str, image: str) -> tuple[str, str, str | None]:
    """Map a compose image reference to (key, repository)."""
    for var, rule in rules["variables"].items():
        if "image" in rule and ("$" + var in image or "${" + var in image):
            key = rule["image"]
            if key == "posthog":
                repo = "posthog/posthog-node" if image.endswith("-node:${POSTHOG_NODE_TAG:-latest}") else "posthog/posthog"
                if repo.endswith("-node"):
                    key = "posthog-node"
            elif key == "clickhouse":
                default = re.search(r":-([^}]+)}", image).group(1)
                repo, tag = default.rsplit(":", 1)
                return key, repo, tag
            else:
                raise SyncError(f"{service}: unhandled image variable in {image}")
            return key, repo, None
    repo, _, _tag = image.partition("@")[0].rpartition(":") if ":" in image.split("/")[-1] else (image, "", "")
    return service, repo or image, None


def image_tag(image: str) -> str:
    last = image.split("/")[-1]
    return last.split(":", 1)[1] if ":" in last else "latest"


def resolve_digest(repository: str, tag: str) -> str | None:
    """Resolve repository:tag to a manifest digest via the registry v2 API (anonymous)."""
    first = repository.split("/")[0]
    if "/" not in repository or ("." not in first and ":" not in first):
        host, path = "registry-1.docker.io", repository if "/" in repository else f"library/{repository}"
    else:
        host, path = repository.split("/", 1)
    if host == "docker.io":
        host = "registry-1.docker.io"
        if "/" not in path:
            path = f"library/{path}"
    url = f"https://{host}/v2/{path}/manifests/{tag}"
    accept = ", ".join(
        [
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ]
    )
    headers = {"Accept": accept, "User-Agent": "posthog-k8s-sync"}
    try:
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            return r.headers.get("Docker-Content-Digest")
    except urllib.error.HTTPError as e:
        if e.code != 401:
            return None
        auth = e.headers.get("WWW-Authenticate", "")
        m = re.search(r'realm="([^"]+)",service="([^"]+)"(?:,scope="([^"]+)")?', auth)
        if not m:
            return None
        realm, service, scope = m.groups()
        scope = scope or f"repository:{path}:pull"
        tok = json.loads(urllib.request.urlopen(f"{realm}?service={service}&scope={scope}", timeout=30).read())  # noqa: S310
        headers["Authorization"] = "Bearer " + (tok.get("token") or tok.get("access_token"))
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
                return r.headers.get("Docker-Content-Digest")
        except urllib.error.HTTPError:
            return None
    except urllib.error.URLError:
        return None


# ----------------------------------------------------------------------------- caddy routes


def parse_caddy_routes(caddyfile: str) -> list[dict]:
    """Extract named path matchers and their reverse_proxy targets from upstream's Caddyfile."""
    matchers: dict[str, list[str]] = {}
    for m in re.finditer(r"@([\w-]+)\s*\{([^}]*)\}", caddyfile):
        paths = re.findall(r"^\s*path\s+(\S+)", m.group(2), flags=re.M)
        if paths:
            matchers[m.group(1)] = paths
    routes = []
    for m in re.finditer(r"handle\s+@([\w-]+)\s*\{(.*?)\n\s*\}", caddyfile, flags=re.S):
        name, body = m.group(1), m.group(2)
        target = re.search(r"reverse_proxy\s+([\w.-]+):(\d+)", body)
        if not target:
            raise SyncError(f"caddy handle @{name} without reverse_proxy")
        route = {"name": name, "paths": matchers.get(name, []), "service": target.group(1), "port": int(target.group(2))}
        strip = re.search(r"uri\s+strip_prefix\s+(\S+)", body)
        if strip:
            route["stripPrefix"] = strip.group(1)
        if "flush_interval -1" in body:
            route["streaming"] = True
        routes.append(route)
    fallback = re.search(r"handle\s*\{\s*reverse_proxy\s+([\w.-]+):(\d+)", caddyfile)
    if not fallback:
        raise SyncError("caddy fallback handle not found")
    routes.append({"name": "app", "paths": ["/*"], "service": fallback.group(1), "port": int(fallback.group(2)), "fallback": True})
    return routes


# ----------------------------------------------------------------------------- main build


def build(rules: dict, source: Source, want_digests: bool) -> tuple[dict, dict[str, bytes]]:
    services = load_compose(source, rules["upstream"]["compose"])
    env_file = env_to_dict([l for l in source.file(rules["upstream"]["env_file"]).decode().splitlines() if l and not l.startswith("#")])
    rw = Rewriter(rules)
    out_services: dict[str, dict] = {}
    images: dict[str, dict] = {}
    known = set(rules["excluded"]) | set(rules["stateful"]) | set(rules["jobs"]) | set(rules["ports"]) | set(rules.get("sidecars", {}))

    for name, svc in services.items():
        if name in rules["excluded"]:
            continue
        if name not in known:
            rw.problems.append(f"service {name!r} is new upstream: add it to rules.yaml (ports/stateful/jobs/excluded)")
            continue
        if svc.get("build") and not svc.get("image"):
            rw.problems.append(f"{name}: build-only service without image")
            continue
        key, repo, fixed_tag = image_key(rules, name, svc["image"])
        images.setdefault(key, {"repository": repo, "tag": fixed_tag or (image_tag(svc["image"]) if "$" not in svc["image"] else None)})

        raw_env = dict(svc.get("environment") or {})
        if svc.get("env_file"):
            raw_env = {**env_file, **raw_env}
        for dropped in rules.get("env_drop", {}).get(name, []):
            raw_env.pop(dropped, None)
        env = rw.env(name, raw_env)
        present = {e.name for e in env}
        for trigger, companions in rules.get("env_companions", {}).items():
            if trigger in present:
                for c in companions:
                    if c["name"] not in present:
                        env.append(Env(c["name"], value=c.get("value"), secret=c.get("secret"), tpl=c.get("tpl", False)))
                        present.add(c["name"])
        for extra in rules["env_extra"].get(name, []):
            env = [e for e in env if e.name != extra["name"]]
            env.append(Env(extra["name"], value=extra.get("value"), secret=extra.get("secret"), tpl=extra.get("tpl", False)))

        command = rules["commands"].get(name)
        args = None
        cmd_tpl = False
        if command is None:
            entrypoint = split_command(svc.get("entrypoint"))
            cmd = split_command(svc.get("command"))
            if entrypoint is not None:
                command, cmd_tpl = rw.command(name, entrypoint)
                args, a_tpl = rw.command(name, cmd)
                cmd_tpl = cmd_tpl or a_tpl
            elif cmd is not None:
                args, cmd_tpl = rw.command(name, cmd)

        ports = rules["ports"].get(name, {})
        port_list = [{"name": pn, "port": pv} for pn, pv in ports.items() if pn != "metrics"]
        kind = "job" if name in rules["jobs"] else "statefulset" if name in rules["stateful"] else "deployment"
        if name in rules.get("sidecars", {}):
            kind = "sidecar"
        entry: dict[str, Any] = {
            "kind": kind,
            "image": key,
            "ports": port_list,
            "env": [e.out() for e in env],
        }
        if "metrics" in ports:
            entry["metricsPort"] = ports["metrics"]
        if command is not None:
            entry["command"] = command
        if args is not None:
            entry["args"] = args
        if cmd_tpl:
            entry["commandTpl"] = True
        if name in rules["probes"]:
            entry["readinessProbe"] = rules["probes"][name]
        elif port_list:
            entry["readinessProbe"] = {"tcpSocket": {"port": port_list[0]["name"]}}
        entry["resources"] = rules["resources"].get(name, rules["resources"]["default"])
        entry["securityContext"] = rules["run_as"].get(name, rules["run_as"]["default"])
        if name in rules["stateful"]:
            entry["persistence"] = rules["stateful"][name]
        if name in rules["jobs"]:
            entry["job"] = rules["jobs"][name]
        if name in rules.get("sidecars", {}):
            entry["sidecarOf"] = rules["sidecars"][name]
        if name in rules["mounts"]:
            entry["mounts"] = rules["mounts"][name]
        deps = svc.get("depends_on") or []
        entry["dependsOn"] = sorted(deps if isinstance(deps, list) else list(deps))
        for vol in svc.get("volumes") or []:
            src = str(vol).split(":")[0]
            if src.startswith("./") and name not in rules["mounts"] and "share" not in src and "compose" not in src:
                rw.problems.append(f"{name}: host mount {vol} has no rule in mounts")
        out_services[name] = entry

    # Router routes from upstream's Caddyfile (embedded as env in the proxy service).
    routes = parse_caddy_routes(services["proxy"]["environment"]["CADDYFILE"])
    for r in routes:
        if r["service"] in rules["excluded"] and not r.get("fallback"):
            r["excluded"] = True
        elif r["service"] not in out_services and r["service"] not in ("web",):
            r["optional"] = True  # e.g. capture-ai: defined in base, not in hobby

    # Images: digests for reproducibility.
    for key, img in images.items():
        if img["tag"] is None:
            # posthog/posthog publishes every master commit as a tag; the node and Rust images only
            # carry moving tags (latest/master), so those are pinned by digest at sync time.
            img["tag"] = source.ref if key == "posthog" else "latest" if key == "posthog-node" else "master"
        if want_digests:
            digest = resolve_digest(img["repository"], img["tag"])
            if digest is None:
                rw.problems.append(f"image {img['repository']}:{img['tag']} not resolvable")
            else:
                img["digest"] = digest

    # Files.
    files: dict[str, bytes] = {}
    for path in rules["upstream"]["files"]:
        files[path] = source.file(path)
    for d in rules["upstream"].get("dirs", []):
        for p in source.listdir(d):
            files[p] = source.file(p)
    for mount_list in rules["mounts"].values():
        for m in mount_list:
            if "dir" in m:
                for p in source.listdir(m["dir"]):
                    files[p] = source.file(p)

    if rw.problems:
        raise SyncError("upstream changed in ways the rules do not cover:\n  - " + "\n  - ".join(sorted(set(rw.problems))))

    # Compose hostnames the code assumes as defaults (PGHOST=db, CLICKHOUSE_HOST=clickhouse, ...).
    # The chart renders ExternalName aliases for them so hidden defaults resolve in Kubernetes too.
    aliases = {}
    for host, rule in rules["hosts"].items():
        if "helper" in rule:
            aliases[host] = {"helper": rule["helper"]}
        elif "service" in rule and rule["service"] in out_services:
            aliases[host] = {"service": rule["service"]}
        elif rule.get("storage"):
            aliases[host] = {"storage": True}

    data = {
        "upstream": {
            "repo": f"https://github.com/{source.repo}",
            "ref": source.ref,
            "composeFiles": rules["upstream"]["compose"],
        },
        "images": dict(sorted(images.items())),
        "services": dict(sorted(out_services.items())),
        "excluded": rules["excluded"],
        "routes": routes,
        "aliases": aliases,
        "imageDirs": rules["upstream"]["image_dirs"],
    }
    return data, files


def write_outputs(data: dict, files: dict[str, bytes], source: Source) -> None:
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    header = (
        "# GENERATED by tools/sync_upstream.py from PostHog/posthog@%s. Do not edit; edit tools/rules.yaml\n"
        "# and re-run `make sync`. Values containing {{ }} are rendered with tpl by the chart templates.\n" % source.ref
    )
    (CHART_DIR / "upstream.yaml").write_text(header + yaml.safe_dump(data, sort_keys=False, width=120))
    if FILES_DIR.exists():
        for p in FILES_DIR.rglob("*"):
            if p.is_file():
                p.unlink()
    for path, content in files.items():
        dest = FILES_DIR / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    lock = {"ref": source.ref, "inputs": dict(sorted(source.hashes.items()))}
    (CHART_DIR / "upstream.lock").write_text(yaml.safe_dump(lock, sort_keys=False))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", required=True, help="PostHog/posthog commit sha")
    ap.add_argument("--cache", default=str(ROOT / ".cache" / "upstream"))
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-digests", action="store_true", help="skip registry lookups")
    ap.add_argument("--check", action="store_true", help="do not write; fail when the committed upstream.yaml differs (digests ignored)")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.ref):
        ap.error("--ref must be a full 40-character commit sha")
    rules = yaml.safe_load(RULES_PATH.read_text())
    source = Source(rules["upstream"]["repo"], args.ref, Path(args.cache), args.offline)
    try:
        data, files = build(rules, source, want_digests=not args.no_digests)
    except SyncError as e:
        print(f"sync failed: {e}", file=sys.stderr)
        return 1
    if args.check:
        current = yaml.safe_load((CHART_DIR / "upstream.yaml").read_text()) if (CHART_DIR / "upstream.yaml").exists() else {}
        strip = lambda d: {k: {kk: vv for kk, vv in v.items() if kk != "digest"} for k, v in d.get("images", {}).items()}  # noqa: E731
        cur_cmp = {**current, "images": strip(current)}
        new_cmp = {**data, "images": strip(data)}
        if cur_cmp != new_cmp:
            import difflib

            diff = difflib.unified_diff(
                yaml.safe_dump(cur_cmp, sort_keys=False, width=120).splitlines(),
                yaml.safe_dump(new_cmp, sort_keys=False, width=120).splitlines(),
                "upstream.yaml (committed)", "upstream.yaml (generated)", lineterm="", n=2,
            )
            print("\n".join(diff), file=sys.stderr)
            print("sync-check failed: run `make sync` and commit the result", file=sys.stderr)
            return 1
        print("sync-check ok")
        return 0
    write_outputs(data, files, source)
    n = len(data["services"])
    print(f"synced {n} services, {len(data['images'])} images, {len(files)} files from {source.repo}@{source.ref[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
