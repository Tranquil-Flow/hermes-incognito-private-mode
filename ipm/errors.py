"""transform_api_error_classification hook (BUILD_SPEC §3.7).

Provider "no private endpoint" errors (OpenRouter "No endpoints found matching
your data policy", Nous equivalent) become non-retryable with a clear message
while private mode is active. Return shape:
``{"reason": <FailoverReason name>, "retryable": bool, "message": str}``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ipm.errors")

# FailoverReason.provider_policy_blocked: "Aggregator account data/privacy policy
# excluded the only endpoint" (agent/error_classifier.py) — exactly this case.
_REASON = "provider_policy_blocked"

_NO_PRIVATE_ENDPOINT_MARKERS = (
    "no endpoints found matching your data policy",   # OpenRouter (exact phrase only)
    "meets this request's data-use requirement",       # Nous 403 (data_policy_denied)
    "data_policy_denied",
)


def classify_error(kwargs: dict[str, Any], *, mode_active: bool, provider: str | None) -> dict[str, Any] | None:
    """Return a non-retryable classification when the failure is the provider
    refusing the privacy flags, else None (no opinion)."""
    if not mode_active:
        return None
    error_message = str(kwargs.get("error_message") or kwargs.get("error") or "")
    error_body = str(kwargs.get("error_body") or "")
    haystack = (error_message + "\n" + error_body).lower()
    if not any(marker in haystack for marker in _NO_PRIVATE_ENDPOINT_MARKERS):
        return None
    who = provider or kwargs.get("provider") or "this provider"
    return {
        "reason": _REASON,
        "retryable": False,
        "message": (
            f"Private mode: {who} has no endpoint that honours the privacy flags "
            "for this model. Choose a model with a privacy route, or run "
            "/private off for this chat."
        ),
    }
