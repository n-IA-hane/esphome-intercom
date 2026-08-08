"""Transport helpers shared by SIP clients and trunks."""

from __future__ import annotations

import asyncio
import ssl


async def default_tls_context() -> ssl.SSLContext:
    """Load the system trust store without blocking the event loop."""

    return await asyncio.to_thread(ssl.create_default_context)
