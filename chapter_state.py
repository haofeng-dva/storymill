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


def classify_failure(error=None, *, content=None, word_count=None, word_min=None,
                     word_max=None, quality=None, quality_min=None, file_error=False):
    if file_error:
        return FailureType.FILE.value
    if error:
        msg = str(error).lower()
        if any(x in msg for x in ("http", "timeout", "timed out", "urlerror", "connection", "503", "504", "429")):
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
        FailureType.EMPTY_OUTPUT.value,
        FailureType.WORD_COUNT.value,
        FailureType.QUALITY.value,
        FailureType.MODEL.value,
    }


def is_auto_retryable(failure_type):
    """Failures safe for delayed chapter-level recovery after the block run."""
    return failure_type in {
        FailureType.NETWORK.value,
        FailureType.EMPTY_OUTPUT.value,
        FailureType.MODEL.value,
    }
