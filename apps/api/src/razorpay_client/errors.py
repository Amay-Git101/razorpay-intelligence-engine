class RazorpayAPIError(Exception):
    """Raised for any non-2xx Razorpay API response or transport-level
    failure. Never includes credentials -- callers must not add them
    either (see RazorpayReadClient docstring)."""
