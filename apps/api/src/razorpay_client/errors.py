class RazorpayAPIError(Exception):
    """Raised for a non-2xx Razorpay API response. Never includes
    credentials -- callers must not add them either.

    status_code is optional (defaults to None) purely for backward
    compatibility with existing raise sites that don't pass it -- new
    callers (the Gate 8 write client) pass it so orchestration code can
    distinguish a definite error response (this exception) from an
    ambiguous transport failure (a raw httpx.HTTPError, not this type)
    without parsing the message string.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
