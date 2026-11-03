import asyncio

from starlette.background import BackgroundTask
from app.streaming import DisconnectAwareStreamingResponse


def test_disconnect_interrupts_silent_provider_asgi24():
    async def run():
        waiting = asyncio.Event()
        cleaned = asyncio.Event()
        background = asyncio.Event()

        async def body():
            try:
                yield "data: ready\n\n"
                waiting.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        async def receive():
            await waiting.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        response = DisconnectAwareStreamingResponse(body(), background=BackgroundTask(background.set))
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send), 1)
        assert cleaned.is_set()
        assert background.is_set()

    asyncio.run(run())


def test_send_error_closes_suspended_generator():
    async def run():
        cleaned = asyncio.Event()

        async def body():
            try:
                yield "data: ready\n\n"
            finally:
                cleaned.set()

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("closed socket")

        try:
            await DisconnectAwareStreamingResponse(body())({}, receive, send)
        except OSError:
            pass
        assert cleaned.is_set()

    asyncio.run(run())
