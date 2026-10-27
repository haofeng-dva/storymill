"""Small, dependency-free chapter state and recovery policy definitions."""

from enum import Enum


class ChapterStatus(str, Enum):
    PENDING = "pending"
    WRITING = "writing"
    RETRYING = "retrying"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class FailureType(str, Enum):
    NETWORK = "network_error"
    ROUTE_FORBIDDEN = "route_forbidden"
    MODEL_NOT_FOUND = "model_not_found"
    PROXY_TLS = "proxy_tls_error"
    GATEWAY_TIMEOUT = "gateway_timeout"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    ACCEPTANCE_INFRASTRUCTURE = "acceptance_infrastructure_failure"
    EMPTY_OUTPUT = "empty_output"
    WORD_COUNT = "word_count_failure"
    QUALITY = "quality_failure"
    FILE = "file_failure"
    MODEL = "model_error"
    DEPENDENCY = "dependency_blocked"


ACTIVE_STATUSES = {
    ChapterStatus.WRITING.value,
    ChapterStatus.RETRYING.value,
}
FINISHED_STATUSES = {
    ChapterStatus.COMPLETED.value,
    ChapterStatus.FAILED.value,
    ChapterStatus.BLOCKED.value,
}


INFRASTRUCTURE_FAILURE_TYPES = {
    FailureType.NETWORK.value,
    FailureType.ROUTE_FORBIDDEN.value,
    FailureType.MODEL_NOT_FOUND.value,
    FailureType.PROXY_TLS.value,
    FailureType.GATEWAY_TIMEOUT.value,
    FailureType.UPSTREAM_TIMEOUT.value,
    FailureType.ACCEPTANCE_INFRASTRUCTURE.value,
}


TRANSIENT_INFRASTRUCTURE_FAILURE_TYPES = {
    FailureType.NETWORK.value,
    FailureType.GATEWAY_TIMEOUT.value,
}


def classify_failure(error=None, *, content=None, word_count=None, word_min=None,
                     word_max=None, quality=None, quality_min=None, file_error=False):
    if file_error:
        return FailureType.FILE.value
    if error:
        msg = str(error).lower()
        # Bodies take precedence over HTTP status: the relay uses 502 both for
        # a permanently forbidden route and for ordinary gateway failures.
        if "upstream access forbidden" in msg:
            return FailureType.ROUTE_FORBIDDEN.value
        if any(x in msg for x in ("model_not_found", "model not found", "not supported by any configured account")):
            return FailureType.MODEL_NOT_FOUND.value
        if any(x in msg for x in ("ssl", "unexpected_eof", "eof occurred")):
            return FailureType.PROXY_TLS.value
        if any(x in msg for x in ("http 504", "gateway time-out", "gateway timeout")):
            return FailureType.GATEWAY_TIMEOUT.value
        if any(x in msg for x in ("timeout", "timed out", "http hang")):
            return FailureType.UPSTREAM_TIMEOUT.value
        if any(x in msg for x in (
            "streaminterruptederror", "stream ended without a terminal event",
            "http", "urlerror", "connection", "503", "502", "429",
        )):
            return FailureType.NETWORK.value
        return FailureType.MODEL.value
    if not (content or "").strip():
        return FailureType.EMPTY_OUTPUT.value
    if word_count is not None and word_min is not None and word_max is not None and not (word_min <= word_count <= word_max):
        return FailureType.WORD_COUNT.value
    if quality is not None and quality_min is not None and quality < quality_min:
        return FailureType.QUALITY.value
    return None


def is_recoverable(failure_type):
    return failure_type in {
        FailureType.NETWORK.value,
        FailureType.GATEWAY_TIMEOUT.value,
        FailureType.EMPTY_OUTPUT.value,
        FailureType.WORD_COUNT.value,
        FailureType.QUALITY.value,
        FailureType.MODEL.value,
    }


def is_auto_retryable(failure_type):
    """Failures safe for delayed chapter-level recovery after the block run."""
    return failure_type in {
        FailureType.NETWORK.value,
        FailureType.GATEWAY_TIMEOUT.value,
    }


def is_infrastructure_failure(failure_type):
    return failure_type in INFRASTRUCTURE_FAILURE_TYPES


def is_transient_infrastructure_failure(failure_type):
    return failure_type in TRANSIENT_INFRASTRUCTURE_FAILURE_TYPES
