"""Errors with stable codes, so the dashboard can show them in the viewer's language."""

from __future__ import annotations

from typing import Any

# Every code the API can return. The dashboard has a translated message for each (err.<code>).
ALL_CODES = (
    "bad_request", "not_signed_in", "wrong_password", "too_many_attempts", "already_set_up",
    "password_too_short", "temperature_invalid", "price_invalid", "rpm_invalid",
    "term_required", "term_too_long", "term_exists", "term_not_found", "file_empty",
    "sample_too_short", "text_required", "text_too_long", "too_many_paragraphs", "wrong_team_key",
    "not_configured", "upstream_rate_limit", "upstream_unavailable", "upstream_auth",
    "upstream_not_found", "upstream_unreachable", "upstream_bad_response", "upstream_error",
    "suggest_failed", "internal_error",
    # glossary safety (v0.3)
    "term_warning", "condition_needs_note", "history_not_found", "history_conflict", "already_undone",
    "proposal_not_found", "proposal_decided", "too_many_proposals", "proposals_full", "name_too_long",
    # glossary sets and links (v0.4)
    "set_not_found", "set_exists", "set_protected", "set_not_empty", "link_not_found", "link_protected",
    "link_needs_set", "name_required",
)


class AppError(Exception):
    """An error caused by the request (bad input, not signed in, ...)."""

    def __init__(self, status: int, code: str, message: str, **params: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.params = params


class LLMError(Exception):
    """An error talking to the upstream AI provider.

    status      HTTP status Glossa returns to its own client
    code        stable code for the dashboard (upstream_rate_limit, ...)
    raw         the provider's own message, kept for debugging
    retry_after seconds the provider asked us to wait, if it said so
    retryable   whether trying again later can help
    upstream    the provider's HTTP status (0 when it could not be reached)
    """

    def __init__(
        self,
        message: str,
        status: int = 502,
        code: str = "upstream_error",
        raw: str = "",
        retry_after: float | None = None,
        retryable: bool = False,
        upstream: int = 0,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.raw = raw or message
        self.retry_after = retry_after
        self.retryable = retryable
        self.upstream = upstream


def from_upstream_status(status: int, message: str, raw: str, retry_after: float | None) -> LLMError:
    if status == 429:
        return LLMError(message, 429, "upstream_rate_limit", raw, retry_after, True, status)
    if status in (500, 502, 503, 504):
        return LLMError(message, 503, "upstream_unavailable", raw, retry_after, True, status)
    if status in (401, 403):
        return LLMError(message, 502, "upstream_auth", raw, None, False, status)
    if status == 404:
        return LLMError(message, 502, "upstream_not_found", raw, None, False, status)
    return LLMError(message, 502, "upstream_error", raw, None, False, status)
