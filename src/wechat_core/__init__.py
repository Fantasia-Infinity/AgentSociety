"""Channel-neutral WeChat bot core.

.. deprecated::
    This package (LLM auto-reply pipeline for WeChat) is deprecated.

    Local agents now operate WeChat directly through the ``wechatd`` daemon
    and the ``agent_channel`` MCP tools; the Core's model routing, automatic
    replies, and HTTP event pipeline are no longer used. The code is kept for
    reference only and is not expected to receive further updates.
"""

import warnings

warnings.warn(
    "wechat_core is deprecated; use wechatd + agent_channel instead",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["__version__"]

__version__ = "0.2.0"
