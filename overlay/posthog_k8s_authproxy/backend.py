from django.contrib.auth.backends import ModelBackend


class AuthProxyBackend(ModelBackend):
    """Names the sessions the proxy login opens. It never checks credentials itself."""

    def authenticate(self, request, **credentials):
        return None
