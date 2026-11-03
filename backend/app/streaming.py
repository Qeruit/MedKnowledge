"""Disconnect-aware SSE, including while the upstream model is silent."""

import asyncio

from starlette.responses import StreamingResponse


class DisconnectAwareStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        # ASGI 2.4's send-error detection alone cannot interrupt an upstream
        # await before its next token. Always observe http.disconnect as well.
        stream = asyncio.create_task(self.stream_response(send))
        disconnect = asyncio.create_task(self.listen_for_disconnect(receive))
        try:
            done, _ = await asyncio.wait((stream, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if stream in done:
                await stream
        finally:
            for task in (stream, disconnect):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stream, disconnect, return_exceptions=True)
            # A failed send can leave the generator suspended at a yield.
            if hasattr(self.body_iterator, "aclose"):
                await self.body_iterator.aclose()
            if self.background is not None:
                await self.background()
