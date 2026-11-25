"""Domain errors. Services raise these; one handler in main.py turns them into HTTP answers."""


class ThetaError(Exception):
    status_code = 400


class NotFound(ThetaError):
    status_code = 404


class Forbidden(ThetaError):
    status_code = 403


class Conflict(ThetaError):
    status_code = 409


class Unprocessable(ThetaError):
    status_code = 422


class Gone(ThetaError):
    """Something Theta's provider held (an uploaded textbook) is no longer there."""

    status_code = 410


class BadGateway(ThetaError):
    """A service Theta depends on answered, but not usefully."""

    status_code = 502


class ServiceUnavailable(ThetaError):
    """Not set up on this server, or a service Theta depends on is busy."""

    status_code = 503
