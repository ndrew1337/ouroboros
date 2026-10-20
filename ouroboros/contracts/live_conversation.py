"""Live Conversation's transport vocabulary, independent of providers and devices.

The admitted host supplies the complete selected input. A session's ``wire``
receipt describes its physical setup before opening; ``open`` may then use its
granted credential. ``receive`` returns ordered events; ``send`` accepts the
commands below. A renewal request returns to ordinary host fitting. Providers
never select another context or silently discard input.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from ouroboros.contracts.realtime import RealtimeInput


@dataclass(frozen=True)
class LiveOptions:
    voice: str = ""
    # Fresh exact-route response-limit evidence supplied by the host. This is
    # independent of the selected allowance; absent evidence is never a default.
    model_output_bound_tokens: int | None = None


@dataclass(frozen=True)
class LiveEvent:
    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Audio:
    pcm: bytes
    rate: int


@dataclass(frozen=True)
class Video:
    data: bytes
    mime: str


@dataclass(frozen=True)
class Playback:
    item_id: str
    played_ms: int


@dataclass(frozen=True)
class Interrupt:
    item_id: str = ""
    played_ms: int = 0


@dataclass(frozen=True)
class ToolOutputs:
    # Complete changes from ordinary handle_tool_calls, including image blocks.
    messages: tuple[dict, ...]
    tools: tuple[dict, ...]


@dataclass(frozen=True)
class ContextUpdate:
    messages: tuple[dict, ...]


class LiveSession(Protocol):
    selected: RealtimeInput
    wire: dict
    # Effective native bound from the wire limiter / evidenced model maximum.
    # None means unknown, including an unbounded field with no fresh maximum.
    output_bound_tokens: int | None
    renewal_ready: bool  # native input/transcription work has reached a retained boundary

    def final_captions(self) -> list[LiveEvent]: ...
    async def open(self) -> None: ...
    async def receive(self) -> list[LiveEvent]: ...
    async def send(self, command: Audio | Video | Playback | Interrupt | ToolOutputs | ContextUpdate) -> None: ...
    async def close(self) -> None: ...
