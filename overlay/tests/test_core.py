import json

import pytest
from posthog_k8s_authproxy import core

SECRET = "s3cret-shared-between-the-router-and-web"
ENV = {
    "AUTH_PROXY_ENABLED": "true",
    "AUTH_PROXY_SECRET": SECRET,
    "AUTH_PROXY_SECRET_HEADER": "X-Posthog-K8s-Auth-Proxy",
    "AUTH_PROXY_USER_HEADER": "X-Forwarded-User",
    "AUTH_PROXY_EMAIL_HEADER": "X-Forwarded-Email",
    "AUTH_PROXY_GROUPS_HEADER": "X-Forwarded-Groups",
    "AUTH_PROXY_GROUPS": json.dumps(
        {
            "platform-admins": {"organizations": ["*"], "level": "admin"},
            "acme-developers": {"organizations": ["acme"], "level": "member"},
            "acme-leads": {"organizations": ["acme"], "level": "admin"},
        }
    ),
}


def config(**overrides):
    return core.load_config({**ENV, **overrides})


def headers(*pairs, secret=SECRET):
    out = [(b"host", b"posthog.example.com")]
    if secret is not None:
        out.append((b"x-posthog-k8s-auth-proxy", secret.encode()))
    out.extend((k.lower().encode(), v.encode()) for k, v in pairs)
    return out


class TestLoadConfig:
    def test_off_unless_enabled(self):
        assert core.load_config({}) is None
        assert core.load_config({**ENV, "AUTH_PROXY_ENABLED": "false"}) is None

    def test_reads_headers_lowercased_and_password_login_default(self):
        cfg = config()
        assert cfg.secret_header == "x-posthog-k8s-auth-proxy"
        assert cfg.email_header == "x-forwarded-email"
        assert cfg.password_login is True
        assert cfg.groups["acme-developers"] == core.GroupRule(("acme",), "member")

    def test_password_login_off(self):
        assert config(AUTH_PROXY_PASSWORD_LOGIN="false").password_login is False

    def test_empty_secret_is_an_error(self):
        with pytest.raises(core.ConfigError, match="AUTH_PROXY_SECRET is empty"):
            config(AUTH_PROXY_SECRET="")

    def test_short_secret_is_an_error(self):
        with pytest.raises(core.ConfigError, match="shorter than 32 characters"):
            config(AUTH_PROXY_SECRET="x" * 31)
        assert config(AUTH_PROXY_SECRET="x" * 32).secret == "x" * 32

    @pytest.mark.parametrize("secret", ["x" * 40 + "\n", "x" * 20 + " " + "x" * 20, "x" * 40 + "\u00fc"])
    def test_secret_that_cannot_be_a_header_value_is_an_error(self, secret):
        with pytest.raises(core.ConfigError, match="printable ASCII"):
            config(AUTH_PROXY_SECRET=secret)

    @pytest.mark.parametrize("name", ["", "X_Forwarded_Email", "X Forwarded Email", "-x"])
    def test_bad_header_name_is_an_error(self, name):
        with pytest.raises(core.ConfigError, match="not a header name"):
            config(AUTH_PROXY_EMAIL_HEADER=name)

    def test_shared_header_name_is_an_error(self):
        with pytest.raises(core.ConfigError, match="four different headers"):
            config(AUTH_PROXY_USER_HEADER="x-forwarded-email")

    @pytest.mark.parametrize(
        "groups, message",
        [
            ("{not json", "not JSON"),
            ("[]", "must be an object"),
            ('{"g": {"organizations": [], "level": "admin"}}', "non-empty list"),
            ('{"g": {"organizations": ["acme"], "level": "owner"}}', "member or admin"),
            ('{"g": "acme"}', "non-empty list"),
        ],
    )
    def test_bad_groups_are_an_error(self, groups, message):
        with pytest.raises(core.ConfigError, match=message):
            config(AUTH_PROXY_GROUPS=groups)

    def test_no_groups_is_valid(self):
        assert config(AUTH_PROXY_GROUPS="").groups == {}


class TestReadIdentity:
    def test_reads_email_name_and_groups(self):
        identity = core.read_identity(
            headers(
                ("X-Forwarded-Email", " Jane@Example.com "),
                ("X-Forwarded-User", "jane"),
                ("X-Forwarded-Groups", "b, a,,a"),
            ),
            config(),
        )
        assert identity == core.Identity(email="jane@example.com", name="jane", groups=("a", "b"))

    def test_ignored_without_the_secret(self):
        assert core.read_identity(headers(("X-Forwarded-Email", "jane@example.com"), secret=None), config()) is None

    def test_ignored_with_a_wrong_secret(self):
        assert core.read_identity(headers(("X-Forwarded-Email", "jane@example.com"), secret="guess"), config()) is None

    def test_ignored_when_the_secret_occurs_twice(self):
        raw = headers(("X-Forwarded-Email", "jane@example.com")) + [(b"x-posthog-k8s-auth-proxy", SECRET.encode())]
        assert core.read_identity(raw, config()) is None

    def test_no_email_names_nobody(self):
        assert core.read_identity(headers(("X-Forwarded-Groups", "platform-admins")), config()) is None

    @pytest.mark.parametrize("name", ["X-Forwarded-Email", "X-Forwarded-Groups", "X-Forwarded-User"])
    def test_rejects_a_repeated_identity_header(self, name):
        raw = headers(("X-Forwarded-Email", "jane@example.com"), (name, "x@example.com"), (name, "platform-admins"))
        with pytest.raises(core.Rejected, match="occurs"):
            core.read_identity(raw, config())

    def test_a_repeated_header_without_the_secret_is_not_looked_at(self):
        raw = headers(("X-Forwarded-Groups", "a"), ("X-Forwarded-Groups", "b"), secret=None)
        assert core.read_identity(raw, config()) is None

    @pytest.mark.parametrize(
        "email",
        ["jane", "jane@example.com,eve@example.com", "jane@example.com eve@example.com", "a@" + "b" * 260 + ".com"],
    )
    def test_rejects_what_is_not_one_email_address(self, email):
        with pytest.raises(core.Rejected, match="not one email address"):
            core.read_identity(headers(("X-Forwarded-Email", email)), config())

    @pytest.mark.parametrize(
        "name", ["X_Forwarded_Groups", "x_forwarded-groups", "X_Forwarded_Email", "X_Forwarded_User"]
    )
    def test_rejects_another_spelling_next_to_the_exact_header(self, name):
        raw = headers(
            ("X-Forwarded-Email", "jane@example.com"),
            ("X-Forwarded-User", "jane"),
            ("X-Forwarded-Groups", "acme-developers"),
            (name, "platform-admins"),
        )
        with pytest.raises(core.Rejected, match="occurs 2 times"):
            core.read_identity(raw, config())

    def test_rejects_another_spelling_on_its_own(self):
        raw = headers(("X-Forwarded-Email", "jane@example.com"), ("X_Forwarded_Groups", "platform-admins"))
        with pytest.raises(core.Rejected, match="was sent as x_forwarded_groups"):
            core.read_identity(raw, config())

    def test_rejects_another_spelling_of_the_secret_header_next_to_the_real_one(self):
        raw = headers(("X-Forwarded-Email", "jane@example.com"), ("X_Posthog_K8s_Auth_Proxy", "anything"))
        with pytest.raises(core.Rejected, match="x-posthog-k8s-auth-proxy occurs 2 times"):
            core.read_identity(raw, config())

    def test_the_secret_under_another_spelling_is_not_the_secret(self):
        raw = headers(("X-Forwarded-Email", "jane@example.com"), ("X_Posthog_K8s_Auth_Proxy", SECRET), secret=None)
        assert core.read_identity(raw, config()) is None


class TestForgettingTheSecret:
    def test_without_header_drops_every_spelling_and_nothing_else(self):
        raw = headers(("X-Forwarded-Email", "jane@example.com"), ("X_Posthog_K8s_Auth_Proxy", "x"))
        assert core.without_header(raw, "x-posthog-k8s-auth-proxy") == [
            (b"host", b"posthog.example.com"),
            (b"x-forwarded-email", b"jane@example.com"),
        ]

    def test_meta_key_is_djangos_name_for_the_header(self):
        assert core.meta_key("x-posthog-k8s-auth-proxy") == "HTTP_X_POSTHOG_K8S_AUTH_PROXY"


class TestFingerprint:
    def test_follows_email_groups_and_mapping(self):
        cfg = config()
        jane = core.Identity("jane@example.com", "jane", ("a",))
        assert core.fingerprint(jane, cfg) == core.fingerprint(core.Identity("jane@example.com", "Jane D", ("a",)), cfg)
        assert core.fingerprint(jane, cfg) != core.fingerprint(
            core.Identity("jane@example.com", "jane", ("a", "b")), cfg
        )
        assert core.fingerprint(jane, cfg) != core.fingerprint(core.Identity("eve@example.com", "jane", ("a",)), cfg)
        other_mapping = config(AUTH_PROXY_GROUPS='{"a": {"organizations": ["acme"], "level": "member"}}')
        assert core.fingerprint(jane, cfg) != core.fingerprint(jane, other_mapping)


class TestDesiredLevels:
    def test_unmapped_groups_grant_nothing(self):
        assert core.desired_levels(["everyone"], config()) == {}

    def test_admin_wins_over_member_whatever_the_order(self):
        assert core.desired_levels(["acme-developers", "acme-leads"], config()) == {"acme": "admin"}
        assert core.desired_levels(["acme-leads", "acme-developers"], config()) == {"acme": "admin"}

    def test_wildcard_and_named_organizations_are_kept_apart(self):
        assert core.desired_levels(["platform-admins", "acme-developers"], config()) == {"*": "admin", "acme": "member"}


class TestResolve:
    ORGS = (("0190aaaa-0000-0000-0000-000000000001", "acme"), ("0190AAAA-0000-0000-0000-000000000002", "globex"))

    def test_by_slug_and_by_id(self):
        levels, unknown = core.resolve({"ACME": "member", "0190aaaa-0000-0000-0000-000000000002": "admin"}, self.ORGS)
        assert levels == {
            "0190aaaa-0000-0000-0000-000000000001": "member",
            "0190AAAA-0000-0000-0000-000000000002": "admin",
        }
        assert unknown == []

    def test_wildcard_is_every_organization_and_the_higher_level_wins(self):
        levels, _ = core.resolve({"*": "member", "globex": "admin"}, self.ORGS)
        assert levels == {
            "0190aaaa-0000-0000-0000-000000000001": "member",
            "0190AAAA-0000-0000-0000-000000000002": "admin",
        }
        levels, _ = core.resolve({"*": "admin", "globex": "member"}, self.ORGS)
        assert set(levels.values()) == {"admin"}

    def test_unknown_references_are_reported_and_skipped(self):
        levels, unknown = core.resolve({"initech": "admin", "acme": "member"}, self.ORGS)
        assert levels == {"0190aaaa-0000-0000-0000-000000000001": "member"}
        assert unknown == ["initech"]

    def test_wildcard_on_an_instance_without_organizations_grants_nothing(self):
        assert core.resolve({"*": "admin"}, []) == ({}, [])


class TestPlan:
    def test_new_user_gets_the_desired_memberships(self):
        p = core.plan({}, {"acme": "member"})
        assert (p.add, p.change, p.remove, p.has_access) == ({"acme": "member"}, {}, (), True)

    def test_nothing_desired_and_nothing_held_is_no_access(self):
        assert core.plan({}, {}).has_access is False

    def test_level_follows_the_groups_both_ways(self):
        assert core.plan({"acme": "member"}, {"acme": "admin"}).change == {"acme": "admin"}
        assert core.plan({"acme": "admin"}, {"acme": "member"}).change == {"acme": "member"}

    def test_unchanged_membership_is_left_alone(self):
        p = core.plan({"acme": "member"}, {"acme": "member"})
        assert (p.add, p.change, p.remove) == ({}, {}, ())

    def test_membership_without_a_group_is_removed(self):
        p = core.plan({"acme": "member", "globex": "admin"}, {"acme": "member"})
        assert p.remove == ("globex",)
        assert p.has_access is True

    def test_last_membership_removed_is_no_access(self):
        p = core.plan({"acme": "admin"}, {})
        assert p.remove == ("acme",)
        assert p.has_access is False

    def test_owner_is_never_lowered(self):
        p = core.plan({"acme": "owner"}, {"acme": "member"})
        assert (p.add, p.change, p.remove) == ({}, {}, ())

    def test_owner_is_never_removed_and_keeps_access(self):
        p = core.plan({"acme": "owner", "globex": "member"}, {})
        assert p.remove == ("globex",)
        assert p.has_access is True

    def test_owner_is_never_granted(self):
        with pytest.raises(core.ConfigError):
            config(AUTH_PROXY_GROUPS='{"g": {"organizations": ["acme"], "level": "owner"}}')


class TestPasswordPaths:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/login",
            "/api/login/",
            "/api/login/precheck",
            "/api/login/token/",
            "/api/login/2fa/passkey/begin/",
            "/api/signup",
            "/api/signup/",
            "/api/signup/precheck",
            "/api/signup/0190aaaa-0000-0000-0000-000000000001/",
            "/api/social_signup",
            "/api/reset/",
            "/api/reset/0190aaaa-0000-0000-0000-000000000001/",
            "/api/webauthn/login/begin/",
            "/api/webauthn/signup-register/begin/",
            "/api/users/verify_email/",
            "/api/users/request_email_verification/",
            "/api/login.json",
            "/api/reset.json",
            "/api/partners/stripe/login",
            "/account/login/",
            "/agentic/login",
            "/login/github/",
            "/login/vercel/continue",
            "/complete/github/",
        ],
    )
    def test_ways_in_are_password_paths(self, path):
        assert core.is_password_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/login",
            "/logout",
            "/api/users/@me/",
            "/api/projects/1/events/",
            "/api/loginas",
            "/api/signups",
            "/api/reset_2fa/0190aaaa-0000-0000-0000-000000000001/",
            "/api/users/@me/login_sessions/",
            "/account/two_factor/qrcode/",
            "/login/",
            "/api/webauthn/credentials/",
            "/api/webauthn/register/begin/",
            "/api/organizations/@current/invites/",
            "/complete/github-link/",
            "/complete/slack-link/",
            "/complete/slack-link/start/",
            "/_health",
            "/static/app.js",
        ],
    )
    def test_everything_else_is_not(self, path):
        assert not core.is_password_path(path)
