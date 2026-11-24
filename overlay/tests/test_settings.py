import pytest
from posthog_k8s_authproxy import core, settings
from test_core import ENV


def django_settings():
    return {
        "MIDDLEWARE": [
            "django.contrib.sessions.middleware.SessionMiddleware",
            "posthog.middleware.AuthenticationMiddleware",
            "posthog.middleware.AdminImpersonationMiddleware",
        ],
        "AUTHENTICATION_BACKENDS": ["django.contrib.auth.backends.ModelBackend"],
    }


def test_does_nothing_while_the_login_is_off():
    s = django_settings()
    settings.apply(s, {})
    assert s == django_settings()


def test_installs_the_middleware_right_after_authentication_and_the_backend_in_place():
    s = django_settings()
    backends = s["AUTHENTICATION_BACKENDS"]
    settings.apply(s, dict(ENV))
    assert s["MIDDLEWARE"] == [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "posthog.middleware.AuthenticationMiddleware",
        settings.MIDDLEWARE,
        "posthog.middleware.AdminImpersonationMiddleware",
    ]
    assert s["AUTHENTICATION_BACKENDS"] is backends
    assert backends[-1] == settings.BACKEND


def test_applying_twice_changes_nothing():
    s = django_settings()
    settings.apply(s, dict(ENV))
    once = {k: list(v) for k, v in s.items()}
    settings.apply(s, dict(ENV))
    assert s == once


def test_refuses_a_posthog_without_the_anchor_middleware():
    s = django_settings()
    s["MIDDLEWARE"].remove("posthog.middleware.AuthenticationMiddleware")
    with pytest.raises(core.ConfigError, match="does not match this PostHog version"):
        settings.apply(s, dict(ENV))


def test_refuses_a_broken_configuration():
    with pytest.raises(core.ConfigError, match="AUTH_PROXY_SECRET is empty"):
        settings.apply(django_settings(), {**ENV, "AUTH_PROXY_SECRET": ""})
