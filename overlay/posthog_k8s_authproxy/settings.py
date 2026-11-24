"""Hooked into the end of posthog/settings/__init__.py by the overlay image."""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from typing import Any

from . import core

MIDDLEWARE = "posthog_k8s_authproxy.middleware.AuthProxyMiddleware"
BACKEND = "posthog_k8s_authproxy.backend.AuthProxyBackend"
AFTER = "posthog.middleware.AuthenticationMiddleware"


def apply(settings: MutableMapping[str, Any], env: MutableMapping[str, str] | None = None) -> None:
    """Add the middleware and the backend when AUTH_PROXY_ENABLED is set; do nothing otherwise.

    A broken configuration raises, so that web does not start instead of serving without the login
    its operator asked for.
    """
    if core.load_config(os.environ if env is None else env) is None:
        return
    middleware = list(settings["MIDDLEWARE"])
    if AFTER not in middleware:
        raise core.ConfigError(f"{AFTER} is not in MIDDLEWARE: the overlay does not match this PostHog version")
    if MIDDLEWARE not in middleware:
        middleware.insert(middleware.index(AFTER) + 1, MIDDLEWARE)
    settings["MIDDLEWARE"] = middleware
    # Appended in place: PostHog decides from this very list which session backends count as SSO.
    if BACKEND not in settings["AUTHENTICATION_BACKENDS"]:
        settings["AUTHENTICATION_BACKENDS"].append(BACKEND)
