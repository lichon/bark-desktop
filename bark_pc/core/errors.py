"""User-facing error taxonomy.

Every failure that can reach a dialog or a status line is represented here with a
message written for a non-technical user.  Adapters map library-specific
exceptions onto these types so that the UI never has to inspect third-party
exception classes.
"""

from __future__ import annotations


class BarkError(Exception):
    """Base class for all application errors with a user-facing message."""

    user_message = "Something went wrong."

    def __init__(self, message: str | None = None, *, detail: str | None = None):
        self.detail = detail
        super().__init__(message or self.user_message)

    @property
    def message(self) -> str:
        return str(self)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.args[0] if self.args else self.user_message


class ServerUnreachable(BarkError):
    def __init__(
        self, host: str = "the server", message: str | None = None, *, detail: str | None = None
    ):
        self.host = host
        super().__init__(
            message or f"Cannot reach {host}. Check the address and your network.",
            detail=detail,
        )


class ServerTimeout(BarkError):
    user_message = "The server did not respond in time."


class ServerRejected(BarkError):
    def __init__(self, status: int = 403, message: str | None = None):
        self.status = status
        super().__init__(
            message or f"The server refused the request ({status}). Registration may be closed."
        )


class ServerError(BarkError):
    user_message = "The server reported an internal error. Try again later."


class RegistrationRejected(BarkError):
    def __init__(self, message: str | None = None):
        super().__init__(message or "The server rejected the registration.")

    @classmethod
    def from_server(cls, message: str) -> RegistrationRejected:
        return cls(f"Server said: {message}")


class ActivationRejected(BarkError):
    user_message = "Apple rejected the device activation. Try again in a minute."


class ActivationUnavailable(BarkError):
    user_message = "Cannot reach Apple's activation service. Check your network and try again."


class TokenRejected(BarkError):
    user_message = "The push token was rejected. The device will be re-activated."


class CourierRejected(BarkError):
    user_message = "The push service rejected the connection."


class StreamLost(BarkError):
    user_message = "The connection to the push service was lost."


class IdentityMissing(BarkError):
    user_message = "Device identity missing. Re-activate this device to receive pushes."


class IdentityUnreadable(BarkError):
    user_message = (
        "The saved device identity cannot be read by this Windows account. Re-activate this device."
    )


class PayloadMalformed(BarkError):
    user_message = "A push payload could not be parsed."


class LinkInvalid(BarkError):
    user_message = "The device key is not valid."


# Convenience tuple used by callers that want to catch "any application error".
APP_ERRORS = (BarkError,)
