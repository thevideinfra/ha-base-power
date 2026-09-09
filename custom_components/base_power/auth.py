"""Clerk authentication handler for Base Power."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
import logging
from typing import Any

import aiohttp

from .const import CLERK_DOMAIN, CLERK_JS_VERSION

_LOGGER = logging.getLogger(__name__)

# Refresh JWT 10 seconds before expiry
JWT_REFRESH_BUFFER = 10
# Only used when the token carries no readable "exp" claim.
JWT_LIFETIME = 60

# Clerk's token endpoint should answer quickly; don't let a hung connection
# eat into the coordinator's poll interval.
_AUTH_TIMEOUT = aiohttp.ClientTimeout(total=30)


class AuthenticationError(Exception):
    """Raised when credentials are rejected and the user must sign in again."""


class TokenRefreshError(Exception):
    """Raised when a token refresh fails for a transient reason.

    Deliberately NOT an AuthenticationError: the coordinator turns
    AuthenticationError into ConfigEntryAuthFailed, which forces the user
    through a fresh emailed OTP. A Clerk 5xx/429 or a network blip should
    just fail this poll and be retried on the next one.
    """


def _jwt_expiry(token: str) -> float | None:
    """Return the "exp" claim of a JWT, or None if it can't be read.

    The signature is irrelevant here -- Clerk verifies it, we only want to
    know when to ask for a new one.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        return float(exp) if exp is not None else None
    except (IndexError, ValueError, TypeError, binascii.Error):
        return None


async def _json_body(
    resp: aiohttp.ClientResponse,
    error_cls: type[Exception] = AuthenticationError,
) -> dict[str, Any]:
    """Parse a JSON body, tolerating a wrong/missing content type.

    Callers pass error_cls so a malformed body on the refresh path is treated
    as transient rather than as dead credentials.
    """
    try:
        body = await resp.json(content_type=None)
    except (ValueError, aiohttp.ClientError) as err:
        raise error_cls(
            f"Clerk returned a non-JSON response ({resp.status})"
        ) from err
    if not isinstance(body, dict):
        raise error_cls("Clerk returned an unexpected response shape")
    return body


# Headers to pass Clerk's bot protection
_CLERK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
}


class BasePowerAuth:
    """Handle Clerk authentication and JWT refresh."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_token: str,
        session_id: str,
        session_jwt: str | None = None,
    ) -> None:
        """Initialize auth handler."""
        self._session = session
        self._client_token = client_token
        self._session_id = session_id
        self._jwt: str | None = session_jwt
        # A JWT persisted in the config entry is usually long expired, but read
        # its claim rather than assuming either way.
        self._jwt_expires_at: float = 0
        if session_jwt:
            exp = _jwt_expiry(session_jwt)
            if exp is not None:
                self._jwt_expires_at = exp - JWT_REFRESH_BUFFER

    @property
    def jwt(self) -> str | None:
        """Get current JWT."""
        return self._jwt

    @property
    def is_token_valid(self) -> bool:
        """Check if current JWT is still valid."""
        return self._jwt is not None and time.time() < self._jwt_expires_at

    async def async_refresh_token(self) -> str:
        """Refresh the JWT from Clerk using native mobile API pattern."""
        url = (
            f"{CLERK_DOMAIN}/v1/client/sessions/"
            f"{self._session_id}/tokens"
            f"?_is_native=1&_clerk_js_version={CLERK_JS_VERSION}"
        )
        headers = {
            **_CLERK_HEADERS,
            "Authorization": self._client_token,
            "x-mobile": "1",
        }

        try:
            async with self._session.post(
                url, headers=headers, timeout=_AUTH_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    # Credentials are genuinely dead -- the user must sign in
                    # again. This is the only case that should trigger reauth.
                    _LOGGER.error("Clerk rejected our credentials: %s", resp.status)
                    raise AuthenticationError(
                        f"Clerk token refresh rejected: {resp.status}"
                    )
                if resp.status != 200:
                    # 429/5xx/edge errors are transient; retry next poll rather
                    # than dragging the user through a new emailed OTP.
                    raise TokenRefreshError(
                        f"Clerk token refresh failed: {resp.status}"
                    )

                # Update client token if Clerk rotates it
                new_token = resp.headers.get("Authorization")
                if new_token:
                    self._client_token = new_token

                data = await _json_body(resp, TokenRefreshError)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise TokenRefreshError(f"Could not reach Clerk: {err}") from err

        jwt = data.get("jwt")
        if not jwt:
            raise TokenRefreshError("Clerk response contained no JWT")

        self._jwt = jwt
        exp = _jwt_expiry(jwt)
        self._jwt_expires_at = (
            exp - JWT_REFRESH_BUFFER
            if exp is not None
            else time.time() + JWT_LIFETIME - JWT_REFRESH_BUFFER
        )
        return jwt

    async def async_ensure_valid_token(self) -> str:
        """Ensure we have a valid JWT, refreshing if needed."""
        if not self.is_token_valid:
            return await self.async_refresh_token()
        return self._jwt

    @staticmethod
    async def async_initiate_sign_in(
        session: aiohttp.ClientSession,
        email: str,
        publishable_key: str,
    ) -> dict[str, Any]:
        """Step 1: Initiate Clerk sign-in with email."""
        url = (
            f"{CLERK_DOMAIN}/v1/client/sign_ins"
            f"?_is_native=1&_clerk_js_version={CLERK_JS_VERSION}"
        )
        headers = {
            **_CLERK_HEADERS,
            "Authorization": f"Bearer {publishable_key}",
            "x-mobile": "1",
        }
        data = {"identifier": email}

        async with session.post(url, headers=headers, data=data) as resp:
            if resp.status != 200:
                _LOGGER.error("Sign-in initiation failed: status=%s", resp.status)
                raise AuthenticationError(f"Sign-in initiation failed: {resp.status}")

            # Client token from Authorization response header
            client_token = resp.headers.get("Authorization", "")
            body = await _json_body(resp)

            # Fallback: extract token from response body if header is empty
            if not client_token:
                client_token = body.get("client", {}).get("id", "")
                _LOGGER.debug(
                    "Authorization header empty, using client id: %s",
                    client_token[:20] if client_token else "none",
                )

            sign_in_id = body.get("response", {}).get("id")
            email_id = None
            first_factors = (
                body.get("response", {})
                .get("supported_first_factors")
                or []
            )
            for factor in first_factors:
                if factor.get("strategy") == "email_code":
                    email_id = factor.get("email_address_id")
                    break

            _LOGGER.debug(
                "Sign-in initiated: id=%s email_id=%s token_len=%d",
                sign_in_id, email_id, len(client_token) if client_token else 0,
            )

            return {
                "sign_in_id": sign_in_id,
                "email_id": email_id,
                "client_token": client_token,
            }

    @staticmethod
    async def async_prepare_first_factor(
        session: aiohttp.ClientSession,
        sign_in_id: str,
        email_id: str,
        client_token: str,
    ) -> dict[str, Any]:
        """Step 2: Send OTP code to email."""
        url = (
            f"{CLERK_DOMAIN}/v1/client/sign_ins/{sign_in_id}/"
            f"prepare_first_factor?_is_native=1&_clerk_js_version={CLERK_JS_VERSION}"
        )
        headers = {
            **_CLERK_HEADERS,
            "Authorization": client_token,
            "x-mobile": "1",
        }
        data = {"strategy": "email_code", "email_address_id": email_id}

        async with session.post(url, headers=headers, data=data) as resp:
            if resp.status != 200:
                text = await resp.text()
                _LOGGER.error(
                    "prepare_first_factor failed: status=%s body=%s",
                    resp.status, text,
                )
                raise AuthenticationError(
                    f"Failed to send verification email: {resp.status}"
                )
            # Capture rotated client token
            updated_token = resp.headers.get("Authorization", client_token)
            _LOGGER.debug(
                "prepare_first_factor token rotated: %s",
                updated_token != client_token,
            )
            return {"client_token": updated_token}

    @staticmethod
    async def async_attempt_first_factor(
        session: aiohttp.ClientSession,
        sign_in_id: str,
        code: str,
        client_token: str,
    ) -> dict[str, Any]:
        """Step 3: Verify OTP code and get session."""
        url = (
            f"{CLERK_DOMAIN}/v1/client/sign_ins/{sign_in_id}/"
            f"attempt_first_factor?_is_native=1&_clerk_js_version={CLERK_JS_VERSION}"
        )
        headers = {
            **_CLERK_HEADERS,
            "Authorization": client_token,
            "x-mobile": "1",
        }
        data = {"strategy": "email_code", "code": code}

        async with session.post(url, headers=headers, data=data) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise AuthenticationError(f"OTP verification failed: {text}")

            # Updated client token from response header
            new_client_token = resp.headers.get("Authorization", client_token)
            body = await _json_body(resp)

            # Clerk can list ended/expired sessions alongside the live one;
            # picking the wrong id means every later token refresh fails.
            sessions = body.get("client", {}).get("sessions", []) or []
            active = next(
                (s for s in sessions if s.get("status") == "active"),
                sessions[0] if sessions else None,
            )

            session_id = None
            session_jwt = None
            if active:
                session_id = active.get("id")
                # Try to get JWT directly from response (avoids /tokens call)
                last_token = active.get("last_active_token", {})
                if isinstance(last_token, dict):
                    session_jwt = last_token.get("jwt")

            # Keys only -- user metadata can hold the account's address and
            # other PII, and these logs end up pasted into bug reports.
            _LOGGER.debug(
                "attempt_first_factor: session_id=%s, token_rotated=%s, "
                "jwt_in_response=%s, sessions=%d, session_keys=%s",
                session_id,
                new_client_token != client_token,
                bool(session_jwt),
                len(sessions),
                list(active.keys()) if active else "none",
            )

            return {
                "session_id": session_id,
                "client_token": new_client_token,
                "session_jwt": session_jwt,
                "user_data": active.get("user", {}) if active else {},
            }
