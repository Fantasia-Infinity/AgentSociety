"""Channel-neutral tools exposed to agents through MCP."""

from .service import (
    ChannelCapabilityError,
    ChannelUnavailableError,
    HttpChannelService,
)

__all__ = [
    "ChannelCapabilityError",
    "ChannelUnavailableError",
    "HttpChannelService",
]
