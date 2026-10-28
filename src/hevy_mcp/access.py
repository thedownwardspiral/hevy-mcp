"""Checks the Cloudflare Access JWT on every request except /health.

Cloudflare Access signs in the user (Managed OAuth) and forwards each request
with a signed JWT in the ``Cf-Access-Jwt-Assertion`` header. The server checks
that JWT itself, so a request that reaches it without passing through Access,
or with a token for someone else, is refused before any tool runs. See
docs/adr/0001-cloudflare-access-for-remote-clients.md.

Signing keys come from the team domain's certs endpoint through the same
injectable transport as Hevy and Vault.
"""

import asyncio
import logging
from time import monotonic

import httpx
import jwt
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from hevy_mcp.config import Settings

logger = logging.getLogger(__name__)

ASSERTION_HEADER = "cf-access-jwt-assertion"
OPEN_PATHS = frozenset({"/health"})
# Tolerated clock difference between Cloudflare and this host.
LEEWAY_SECONDS = 30
# An unknown key id refreshes the keys at most this often, so tokens with
# made-up key ids can't make the server hammer the certs endpoint.
REFRESH_COOLDOWN_SECONDS = 60
# After a failed fetch, no other fetch for this long. Requests meanwhile are
# answered at once, from the cached keys or with a 503, instead of queueing
# behind another slow fetch.
RETRY_AFTER_FAILURE_SECONDS = 5
# Keys are refetched once they're this old, so a key Cloudflare withdraws
# stops being trusted without a restart.
KEYS_MAX_AGE_SECONDS = 3600
CERTS_TIMEOUT_SECONDS = 10


class AccessDenied(Exception):
    """The request has no valid token. Messages never contain token or claim values."""


class AccessUnavailable(Exception):
    """The signing keys could not be fetched, so no token can be checked."""


class AccessVerifier:
    """Verifies Access JWTs for one application and returns the caller's email."""

    def __init__(
        self,
        team_domain: str,
        aud: str,
        allowed_emails: frozenset[str],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._issuer = f"https://{team_domain}"
        self._certs_url = f"{self._issuer}/cdn-cgi/access/certs"
        self._aud = aud
        self._allowed_emails = allowed_emails
        self._transport = transport
        self._keys: dict[str, jwt.PyJWK] | None = None
        self._loaded_at = 0.0
        self._failed_at: float | None = None
        self._unknown_kid_refreshed_at: float | None = None
        self._lock = asyncio.Lock()

    async def verify(self, token: str) -> str:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            raise AccessDenied("malformed token") from None
        if not isinstance(kid, str):
            raise AccessDenied("token has no key id")
        key = await self._key(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._aud,
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AccessDenied(f"invalid token ({type(exc).__name__})") from None
        email = claims.get("email")
        if not isinstance(email, str) or email.lower() not in self._allowed_emails:
            raise AccessDenied("caller is not an allowed email")
        return email

    async def _key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            fetched = False
            if self._keys is None or monotonic() - self._loaded_at >= KEYS_MAX_AGE_SECONDS:
                fetched = await self._refresh()
            # Cloudflare rotates its signing keys, so an unknown id may be a new
            # one; keys fetched just now are already current.
            if (
                not fetched
                and self._keys is not None
                and kid not in self._keys
                and self._may_refresh_for_unknown_kid()
            ):
                self._unknown_kid_refreshed_at = monotonic()
                await self._refresh()
            keys = self._keys
        if keys is None:
            raise AccessUnavailable("Cloudflare Access signing keys are not loaded yet.")
        key = keys.get(kid)
        if key is None:
            raise AccessDenied("token signed with an unknown key")
        return key

    def _may_refresh_for_unknown_kid(self) -> bool:
        last = self._unknown_kid_refreshed_at
        return last is None or monotonic() - last >= REFRESH_COOLDOWN_SECONDS

    async def _refresh(self) -> bool:
        """Refetch the keys and say whether that happened.

        A failure keeps any cached keys and pauses fetching for a few seconds.
        """
        if (
            self._failed_at is not None
            and monotonic() - self._failed_at < RETRY_AFTER_FAILURE_SECONDS
        ):
            return False
        try:
            keys = await self._fetch_keys()
        except AccessUnavailable as exc:
            self._failed_at = monotonic()
            if self._keys is None:
                raise
            logger.warning("%s Keeping the cached keys.", exc)
            return False
        self._keys, self._loaded_at, self._failed_at = keys, monotonic(), None
        return True

    async def _fetch_keys(self) -> dict[str, jwt.PyJWK]:
        async with httpx.AsyncClient(
            timeout=CERTS_TIMEOUT_SECONDS, transport=self._transport
        ) as http:
            try:
                response = await http.get(self._certs_url)
                response.raise_for_status()
                keys = jwt.PyJWKSet.from_dict(response.json()).keys
            except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
                raise AccessUnavailable(
                    f"Could not fetch Cloudflare Access signing keys from {self._certs_url} "
                    f"({type(exc).__name__})."
                ) from None
        return {key.key_id: key for key in keys if key.key_id}


class AccessMiddleware:
    """Refuses every request except /health that lacks a valid Access JWT."""

    def __init__(self, app: ASGIApp, verifier: AccessVerifier) -> None:
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan" or (scope["type"] == "http" and scope["path"] in OPEN_PATHS):
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # Nothing here serves websockets; refuse rather than pass them through.
            await send({"type": "websocket.close", "code": 1008})
            return

        assertion = Headers(scope=scope).get(ASSERTION_HEADER)
        try:
            if not assertion:
                raise AccessDenied("no Access token")
            await self.verifier.verify(assertion)
        except AccessDenied as exc:
            logger.warning("Denied %s %s: %s", scope["method"], scope["path"], exc)
            response = JSONResponse({"error": "Access denied."}, status_code=403)
            await response(scope, receive, send)
            return
        except AccessUnavailable as exc:
            logger.error("Denied %s %s: %s", scope["method"], scope["path"], exc)
            response = JSONResponse({"error": "Authentication is unavailable."}, status_code=503)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def access_middleware(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> list[Middleware]:
    """The HTTP middleware that enforces Access, or none when it isn't configured."""
    # Settings guarantee the Access settings are all set or all unset.
    if settings.cf_access_team_domain is None or settings.cf_access_aud is None:
        logger.warning(
            "Cloudflare Access is not configured: the server accepts requests without "
            "authentication. Use this only for local development."
        )
        return []
    verifier = AccessVerifier(
        settings.cf_access_team_domain,
        settings.cf_access_aud,
        settings.cf_access_allowed_emails,
        transport,
    )
    return [Middleware(AccessMiddleware, verifier=verifier)]
