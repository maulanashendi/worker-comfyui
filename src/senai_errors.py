"""Error taxonomy for protocol senai-worker/1.

ERROR_TABLE mirrors contract/senai-worker-1/pins.yaml `error_codes` exactly;
tests/test_senai_worker.py checks the two stay identical.
"""
from dataclasses import dataclass
import re

PROTOCOL = "senai-worker/1"


@dataclass(frozen=True)
class ErrorSpec:
    type: str
    stage: str
    gpu_work: bool
    infra: bool
    retryable: bool
    refresh: bool


ERROR_TABLE: dict[str, ErrorSpec] = {
    "INVALID_ENVELOPE": ErrorSpec("validation_error", "validate", False, False, False, False),
    "UNSUPPORTED_PROTOCOL": ErrorSpec("validation_error", "validate", False, False, False, False),
    "EDITOR_FORMAT": ErrorSpec("validation_error", "validate", False, False, False, False),
    "NODE_NOT_ALLOWED": ErrorSpec("validation_error", "validate", False, False, False, False),
    "DEADLINE_PASSED": ErrorSpec("validation_error", "validate", False, False, False, False),
    "INPUT_HOST_REJECTED": ErrorSpec("input_error", "fetch", False, False, False, False),
    "INPUT_TOO_LARGE": ErrorSpec("input_error", "fetch", False, False, False, False),
    "INPUT_INVALID": ErrorSpec("input_error", "fetch", False, False, False, False),
    "INPUT_FETCH_FAILED": ErrorSpec("input_error", "fetch", False, False, False, False),
    "MODEL_NOT_IN_MANIFEST": ErrorSpec("model_missing", "preflight", False, False, False, False),
    "MODEL_CACHE_MISSING": ErrorSpec("model_missing", "boot", False, True, False, False),
    "PROMPT_REJECTED": ErrorSpec("prompt_rejected", "submit", False, False, False, False),
    "NODE_EXCEPTION": ErrorSpec("execution_error", "execute", True, False, False, False),
    "CUDA_OOM": ErrorSpec("oom", "execute", True, True, True, True),
    "NO_PROGRESS": ErrorSpec("timeout", "execute", True, True, True, True),
    "EXECUTION_DEADLINE": ErrorSpec("timeout", "execute", True, True, False, True),
    "COMFYUI_UNREACHABLE": ErrorSpec("comfyui_down", "boot", False, True, True, True),
    "COMFYUI_CRASHED": ErrorSpec("comfyui_down", "execute", True, True, True, True),
    "OUTPUT_EMPTY": ErrorSpec("output_error", "collect", True, False, False, False),
    "UPLOAD_FAILED": ErrorSpec("output_error", "upload", True, True, True, False),
    "OUTPUT_NOT_CONFIGURED": ErrorSpec("internal_error", "boot", False, True, False, False),
    "INTERNAL": ErrorSpec("internal_error", "internal", True, True, False, True),
}

_URL_QUERY_RE = re.compile(r"\?(?:[A-Za-z0-9_.~%+-]+=[^\s&]*&?)+")
_AUTH_HEADER_RE = re.compile(r"(?i)authorization:\s*\S+")
_HF_TOKEN_RE = re.compile(r"hf_[A-Za-z0-9]+")
MAX_MESSAGE_LENGTH = 500


def scrub(message: str) -> str:
    """Strip URL query strings, Authorization headers, and HF tokens; cap length."""
    if message is None:
        return ""
    text = str(message)
    text = _URL_QUERY_RE.sub("", text)
    text = _AUTH_HEADER_RE.sub("Authorization: [redacted]", text)
    text = _HF_TOKEN_RE.sub("[redacted]", text)
    return text[:MAX_MESSAGE_LENGTH]


class WorkerError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        node_id: str | None = None,
        class_type: str | None = None,
        node_errors: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.node_id = node_id
        self.class_type = class_type
        self.node_errors = node_errors

    @property
    def spec(self) -> ErrorSpec:
        return ERROR_TABLE[self.code]

    def to_error(self) -> dict:
        spec = self.spec
        error = {
            "type": spec.type,
            "code": self.code,
            "stage": spec.stage,
            "message": scrub(self.message),
            "infra": spec.infra,
            "retryable": spec.retryable,
            "gpu_work": spec.gpu_work,
        }
        if self.node_id is not None:
            error["node_id"] = self.node_id
        if self.class_type is not None:
            error["class_type"] = self.class_type
        if self.node_errors is not None:
            error["node_errors"] = dict(list(self.node_errors.items())[:20])
        return error
