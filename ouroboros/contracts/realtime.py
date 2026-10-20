"""Initial realtime setup boundary. Content selection and authority belong to the host.

No provider windows, context policy, credential values or device controls here.
Providers may refuse a layout and return the reason to the host; only the host
can produce a different ordinary ContextFitPlan. A provider declares its route
identity and reports usage in host axes; fitting and settlement stay with the host.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RealtimeSetupError(ValueError):
    """A setup refusal, never permission to shorten or relabel the input."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


# Host usage axes. A provider maps its own usage grammar to them and reports a
# final response, even a failed one, as ``observe("usage", canonical_json({...}))``;
# an axis it cannot name stays absent (unknown), never zero.
USAGE_AXES = ("prompt_tokens", "completion_tokens", "cached_tokens")


@dataclass(frozen=True)
class RealtimeRoute:
    """Route identity a provider registration declares; the host fits and accounts with it.

    ``accounting_provider`` keys cached capability/response-limit evidence and the
    physical-attempt ledger, ``endpoint`` is the socket the provider opens, and
    ``model_prefix`` turns a native model id into host model identity.
    """

    accounting_provider: str
    endpoint: str
    model_prefix: str

    def __post_init__(self):
        for name in ("accounting_provider", "endpoint", "model_prefix"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise RealtimeSetupError("route", f"{name} must be a nonblank string")


@dataclass(frozen=True)
class RealtimeInput:
    """Immutable host-selected model input, detached from mutable caller dictionaries."""

    model: str
    messages_json: str
    tools_json: str
    output_allowance_tokens: int
    mode: str
    core_sha256: str
    memory_view_sha256: str

    @property
    def sha256(self) -> str:
        from dataclasses import asdict

        return sha256(canonical_json(asdict(self)))


@dataclass(frozen=True)
class RealtimeProbe:
    """One host-authorized diagnostic turn; separate from the selected setup input."""

    text: str
    max_output_tokens: int

    def __post_init__(self):
        if not isinstance(self.text, str) or not self.text.strip():
            raise RealtimeSetupError("probe", "a nonempty host-authored probe is required")
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise RealtimeSetupError("probe", "max_output_tokens must be a positive integer")


@dataclass(frozen=True)
class RealtimeSetupResult:
    state: str
    input_sha256: str
    wire_sha256: str
    wire_json: str
    # The actual response.done object, including usage and status. No inferred cost
    # or automatic semantic PASS: the host assesses recall and settles usage.
    response_json: str = ""
