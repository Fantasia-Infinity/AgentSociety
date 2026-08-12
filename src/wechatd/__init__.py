"""Windows WeChat daemon exposing a local HTTP API for agents.

Keeps the WeChat client connected through wxauto (re-login recovery, history
replay, dedup) and lets local agents operate WeChat through plain HTTP
endpoints instead of the retired Core/Gateway upload pipeline.
"""

__all__ = ["__version__"]

__version__ = "0.3.0"
