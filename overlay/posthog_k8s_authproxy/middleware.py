from __future__ import annotations

import os

import structlog
from django.contrib.auth import login, logout
from django.http import HttpRequest, HttpResponse, JsonResponse
from loginas.utils import is_impersonated_session
from posthog.helpers.impersonation import get_original_user_from_session

from . import core, store

logger = structlog.get_logger(__name__)

BACKEND = "posthog_k8s_authproxy.backend.AuthProxyBackend"
SESSION_KEY = "auth_proxy_fingerprint"


def _refuse(request: HttpRequest, status: int, code: str, detail: str) -> HttpResponse:
    if request.path.startswith("/api/"):
        return JsonResponse({"type": "authentication_error", "code": code, "detail": detail}, status=status)
    return HttpResponse(detail, status=status, content_type="text/plain; charset=utf-8")


class AuthProxyMiddleware:
    """Logs in the user the proxy asserts and keeps the memberships in line with the groups.

    Runs right after PostHog's AuthenticationMiddleware. Requests without the router's shared
    secret pass through untouched: for them the identity headers do not exist.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        config = core.load_config(os.environ)
        if config is None:
            raise core.ConfigError("AuthProxyMiddleware is installed but AUTH_PROXY_ENABLED is not set")
        self.config = config

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # The raw header list keeps repeated headers apart; request.META has them joined already.
        # Only ASGI requests carry it, and web serves nothing else.
        headers = getattr(request, "scope", {}).get("headers")
        self._forget_secret(request, headers)

        if not self.config.password_login and core.is_password_path(request.path):
            return _refuse(request, 403, "password_login_disabled", "Log in through the single sign-on proxy.")
        if headers is None:
            return self.get_response(request)
        try:
            identity = core.read_identity(headers, self.config)
        except core.Rejected as e:
            logger.warning("auth_proxy_rejected", reason=str(e))
            return _refuse(
                request, 400, "auth_proxy_bad_headers", "The identity headers of this request are not valid."
            )
        if identity is None:
            return self.get_response(request)

        fingerprint = core.fingerprint(identity, self.config)
        if not self._session_is_current(request, identity, fingerprint):
            user = store.reconcile(identity, self.config)
            if user is None:
                if request.user.is_authenticated:
                    logout(request)
                return _refuse(
                    request, 403, "auth_proxy_no_access", "Your account has no access to this PostHog instance."
                )
            # A session that is this user's already is kept: login() would rotate the CSRF token
            # under the request that happens to carry a changed group list.
            if not request.user.is_authenticated or request.user.pk != user.pk:
                if request.user.is_authenticated:
                    logout(request)
                login(request, user, backend=BACKEND)
                # PostHog's AuthenticationMiddleware reads the user back from this attribute.
                request._cached_user = user
            request.session[SESSION_KEY] = fingerprint
        return self.get_response(request)

    def _forget_secret(self, request: HttpRequest, headers) -> None:
        """Take the shared secret off the request, so that views, logs and error reports never see it."""
        name = self.config.secret_header
        if headers is not None:
            request.scope["headers"] = core.without_header(headers, name)
        request.META.pop(core.meta_key(name), None)
        request.__dict__.pop("headers", None)

    @staticmethod
    def _session_is_current(request: HttpRequest, identity: core.Identity, fingerprint: str) -> bool:
        if not request.user.is_authenticated:
            return False
        if is_impersonated_session(request):
            # Starting an impersonation replaces the session, fingerprint included. The staff
            # member behind it is still the person the proxy names.
            original = get_original_user_from_session(request)
            return original is not None and original.email.lower() == identity.email
        return request.session.get(SESSION_KEY) == fingerprint and request.user.email.lower() == identity.email
