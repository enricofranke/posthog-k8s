"""Decisions of the trusted-header login, free of Django so they can be tested on their own."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

MEMBER = "member"
ADMIN = "admin"
OWNER = "owner"
ALL_ORGANIZATIONS = "*"

_RANK = {MEMBER: 1, ADMIN: 2}
_HEADER_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
# A comma is refused on purpose: a proxy that folds a repeated header into one line joins with it.
_EMAIL = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")
_SECRET = re.compile(r"^[!-~]+\Z")
_MAX_EMAIL = 254
_MAX_NAME = 150
MIN_SECRET_LENGTH = 32

# PostHog's own ways into an account, as routed at the pinned upstream commit. DRF also serves its
# routes with a format suffix (/api/login.json). /login is the page itself; below it are the social
# and partner logins. /complete/<backend>/ is the social login callback, while the github-link and
# slack-link callbacks under it connect an integration to a logged-in user.
_PASSWORD_PATHS = (
    re.compile(r"^/api/login([/.]|$)"),
    re.compile(r"^/api/signup([/.]|$)"),
    re.compile(r"^/api/social_signup([/.]|$)"),
    re.compile(r"^/api/reset([/.]|$)"),
    re.compile(r"^/api/webauthn/(login|signup-register)([/.]|$)"),
    re.compile(r"^/api/users/(verify_email|request_email_verification)([/.]|$)"),
    re.compile(r"^/api/partners/[^/]+/login([/.]|$)"),
    re.compile(r"^/account/login([/.]|$)"),
    re.compile(r"^/agentic/login([/.]|$)"),
    re.compile(r"^/login/[^/]"),
    re.compile(r"^/complete/(?!github-link/?$|slack-link(/|$))[^/]+/?$"),
)


class ConfigError(Exception):
    pass


class Rejected(Exception):
    """The request claims to come through the proxy but cannot be trusted as it is."""


@dataclass(frozen=True)
class GroupRule:
    organizations: tuple[str, ...]
    level: str


@dataclass(frozen=True)
class Config:
    secret: str
    secret_header: str
    user_header: str
    email_header: str
    groups_header: str
    groups: Mapping[str, GroupRule]
    password_login: bool
    mapping_digest: str = field(default="", compare=False)


@dataclass(frozen=True)
class Identity:
    email: str
    name: str
    groups: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    add: Mapping[str, str]
    change: Mapping[str, str]
    remove: tuple[str, ...]
    has_access: bool


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _header(env: Mapping[str, str], key: str) -> str:
    name = (env.get(key) or "").strip().lower()
    if not _HEADER_NAME.match(name):
        raise ConfigError(f"{key} is not a header name: {name!r}")
    return name


def _groups(raw: str) -> dict[str, GroupRule]:
    try:
        parsed = json.loads(raw or "{}")
    except ValueError as e:
        raise ConfigError(f"AUTH_PROXY_GROUPS is not JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ConfigError("AUTH_PROXY_GROUPS must be an object of group -> {organizations, level}")
    rules = {}
    for group, rule in parsed.items():
        organizations = rule.get("organizations") if isinstance(rule, dict) else None
        level = rule.get("level") if isinstance(rule, dict) else None
        if (
            not isinstance(organizations, list)
            or not organizations
            or not all(isinstance(o, str) and o.strip() for o in organizations)
        ):
            raise ConfigError(f"AUTH_PROXY_GROUPS[{group!r}].organizations must be a non-empty list of slugs or IDs")
        if level not in _RANK:
            raise ConfigError(f"AUTH_PROXY_GROUPS[{group!r}].level must be member or admin")
        rules[group] = GroupRule(tuple(o.strip() for o in organizations), level)
    return rules


def load_config(env: Mapping[str, str]) -> Config | None:
    """None while the login is off; ConfigError when it is on and cannot be trusted to work."""
    if not _flag(env.get("AUTH_PROXY_ENABLED"), False):
        return None
    secret = env.get("AUTH_PROXY_SECRET") or ""
    if not secret:
        raise ConfigError("AUTH_PROXY_SECRET is empty")
    if len(secret) < MIN_SECRET_LENGTH:
        raise ConfigError(f"AUTH_PROXY_SECRET is shorter than {MIN_SECRET_LENGTH} characters")
    if not _SECRET.match(secret):
        raise ConfigError("AUTH_PROXY_SECRET must be printable ASCII without spaces or line breaks")
    headers = [
        _header(env, key)
        for key in (
            "AUTH_PROXY_SECRET_HEADER",
            "AUTH_PROXY_USER_HEADER",
            "AUTH_PROXY_EMAIL_HEADER",
            "AUTH_PROXY_GROUPS_HEADER",
        )
    ]
    if len(set(headers)) != len(headers):
        raise ConfigError("the secret, user, email and groups headers must be four different headers")
    groups = _groups(env.get("AUTH_PROXY_GROUPS") or "")
    digest = hashlib.sha256(
        json.dumps({g: [list(r.organizations), r.level] for g, r in sorted(groups.items())}).encode()
    ).hexdigest()
    return Config(
        secret=secret,
        secret_header=headers[0],
        user_header=headers[1],
        email_header=headers[2],
        groups_header=headers[3],
        groups=groups,
        password_login=_flag(env.get("AUTH_PROXY_PASSWORD_LOGIN"), True),
        mapping_digest=digest,
    )


def _normalise(name: str) -> str:
    """The name as Django sees it: X_Forwarded_Email and X-Forwarded-Email are one META key."""
    return name.lower().replace("_", "-")


def _spellings(headers: Iterable[tuple[bytes, bytes]], name: str) -> list[tuple[str, str]]:
    """(name as sent, value) of every header that Django would read as `name`."""
    found = []
    for key, value in headers:
        sent = key.decode("latin1").lower()
        if _normalise(sent) == name:
            found.append((sent, value.decode("latin1")))
    return found


def without_header(headers: Iterable[tuple[bytes, bytes]], name: str) -> list[tuple[bytes, bytes]]:
    """The header list without `name` in any spelling."""
    return [(key, value) for key, value in headers if _normalise(key.decode("latin1")) != name]


def meta_key(name: str) -> str:
    return "HTTP_" + name.upper().replace("-", "_")


def read_identity(headers: Iterable[tuple[bytes, bytes]], config: Config) -> Identity | None:
    """The identity the proxy asserts, from the raw header list of the request.

    None when the request did not come through the router's app listener or names nobody; the
    identity headers are then not looked at. Rejected when it came through the listener and one of
    the headers arrives more than once or under another spelling: the router and the proxy set
    each exactly once under its exact name, so anything else was sent by the client.
    """
    headers = list(headers)
    secrets = _spellings(headers, config.secret_header)
    exact = [value for sent, value in secrets if sent == config.secret_header]
    if len(exact) != 1 or not hmac.compare_digest(exact[0].encode("latin1"), config.secret.encode("latin1")):
        return None
    if len(secrets) > 1:
        raise Rejected(f"header {config.secret_header} occurs {len(secrets)} times")

    found = {}
    for name in (config.user_header, config.email_header, config.groups_header):
        spellings = _spellings(headers, name)
        if len(spellings) > 1:
            raise Rejected(f"header {name} occurs {len(spellings)} times")
        if spellings and spellings[0][0] != name:
            raise Rejected(f"header {name} was sent as {spellings[0][0]}")
        found[name] = spellings[0][1].strip() if spellings else ""

    email = found[config.email_header].lower()
    if not email:
        return None
    if len(email) > _MAX_EMAIL or not _EMAIL.match(email):
        raise Rejected(f"header {config.email_header} is not one email address")

    groups = tuple(sorted({g.strip() for g in found[config.groups_header].split(",") if g.strip()}))
    return Identity(email=email, name=found[config.user_header][:_MAX_NAME], groups=groups)


def fingerprint(identity: Identity, config: Config) -> str:
    """Changes when the session has to be reconciled again: other email, other groups, other mapping."""
    payload = json.dumps([identity.email, list(identity.groups), config.mapping_digest])
    return hashlib.sha256(payload.encode()).hexdigest()


def desired_levels(groups: Iterable[str], config: Config) -> dict[str, str]:
    """Organization reference (slug, ID or "*") -> level the groups grant; admin wins over member."""
    out: dict[str, str] = {}
    for group in groups:
        rule = config.groups.get(group)
        if rule is None:
            continue
        for ref in rule.organizations:
            if _RANK[rule.level] > _RANK.get(out.get(ref, ""), 0):
                out[ref] = rule.level
    return out


def resolve(desired: Mapping[str, str], organizations: Iterable[tuple[str, str]]) -> tuple[dict[str, str], list[str]]:
    """Turn references into organization IDs. `organizations` is every (id, slug) of the instance.

    Returns the levels by ID and the references that match no organization; those are skipped,
    the login never creates an organization.
    """
    by_ref: dict[str, str] = {}
    all_ids: list[str] = []
    for org_id, slug in organizations:
        all_ids.append(org_id)
        by_ref[org_id.lower()] = org_id
        by_ref.setdefault(slug.lower(), org_id)

    out: dict[str, str] = {}
    unknown: list[str] = []

    def grant(org_id: str, level: str) -> None:
        if _RANK[level] > _RANK.get(out.get(org_id, ""), 0):
            out[org_id] = level

    for ref, level in desired.items():
        if ref == ALL_ORGANIZATIONS:
            for org_id in all_ids:
                grant(org_id, level)
        elif ref.lower() in by_ref:
            grant(by_ref[ref.lower()], level)
        else:
            unknown.append(ref)
    return out, sorted(unknown)


def plan(current: Mapping[str, str], desired: Mapping[str, str]) -> Plan:
    """Memberships to add, change and remove so that member and admin follow `desired`.

    Owner memberships are left alone in both directions: never granted, never lowered or removed.
    """
    add = {org: level for org, level in desired.items() if org not in current}
    change = {
        org: level
        for org, level in desired.items()
        if org in current and current[org] != OWNER and current[org] != level
    }
    remove = tuple(sorted(org for org, level in current.items() if level != OWNER and org not in desired))
    has_access = bool(desired) or any(level == OWNER for level in current.values())
    return Plan(add=add, change=change, remove=remove, has_access=has_access)


def is_password_path(path: str) -> bool:
    """Login, signup, invite acceptance, password reset, email verification, passkey, social and partner login."""
    return any(p.match(path) for p in _PASSWORD_PATHS)
