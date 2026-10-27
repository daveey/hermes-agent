"""Preserve every failed runtime route, rather than rethrowing only the primary."""
import math

from hermes_cli.auth_constants import AuthError, CODEX_RATE_LIMITED_CODE


class ProviderFallbackError(AuthError):
    """An exhausted chain; individual exceptions remain available for retry policy."""

    def __init__(self, failures: list[tuple[str, str, BaseException]]) -> None:
        self.failures = tuple(failures)
        details = '; '.join(
            f"{'Primary' if i == 0 else 'Fallback'} {provider or 'configured provider'}"
            f"/{model or 'configured model'}: {type(exc).__name__}: {exc}"
            for i, (provider, model, exc) in enumerate(failures)
        )
        # Only classifications shared by every route describe the exhausted chain.
        # In particular, one transient fallback must prevent a sticky auth block
        # or quota classification inherited from the primary.
        errors = [exc for _, _, exc in self.failures]
        all_quota = bool(errors) and all(
            isinstance(exc, AuthError) and not exc.relogin_required
            and exc.code == CODEX_RATE_LIMITED_CODE for exc in errors
        )
        all_relogin = bool(errors) and all(
            isinstance(exc, AuthError) and exc.relogin_required for exc in errors
        )
        retry_after = None
        if all_quota:
            waits = [exc.retry_after for exc in errors]
            # Unknown/invalid windows must not hide a route that may recover sooner.
            if all(isinstance(wait, (int, float)) and not isinstance(wait, bool)
                   and math.isfinite(wait) and wait > 0 for wait in waits):
                retry_after = min(waits)
        super().__init__(
            f'All provider routes failed. {details}',
            code=CODEX_RATE_LIMITED_CODE if all_quota else 'fallback_exhausted',
            relogin_required=all_relogin, retry_after=retry_after,
        )
