from __future__ import annotations

import json
import logging
import sys
from typing import Any

from .api import AgentHubApi
from .auth import trusted_local_context
from .config import HubSettings
from .mcp import McpService
from .object_store import build_object_store
from .store import AgentHubStore


logger = logging.getLogger(__name__)


def main() -> None:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = HubSettings.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    store = AgentHubStore(settings.database_url or settings.state_db)
    api = AgentHubApi(store, build_object_store(settings.object_store_url))
    service = McpService(api)
    context = trusted_local_context()
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": str(exc)},
                }
            else:
                response = service.handle_message(payload, context)
            if response is None:
                continue
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    finally:
        store.close()


if __name__ == "__main__":
    main()
