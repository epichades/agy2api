from typing import Any, AsyncIterator

from fastapi import Response
from fastapi.responses import StreamingResponse
from anyio import CancelScope
from log import log


async def close_async_iterator(iterator: AsyncIterator[Any]) -> None:
    """Release a nested stream when its consumer stops before exhaustion."""
    close = getattr(iterator, "aclose", None)
    if close is not None:
        # ASGI cancellation is level-triggered; cleanup must survive that scope.
        with CancelScope(shield=True):
            try:
                await close()
            except Exception as error:
                log.warning(f"Stream cleanup failed: {type(error).__name__}")


class ManagedStreamingResponse(StreamingResponse):
    """Close suspended body generators even when cancellation occurs during send."""

    async def stream_response(self, send):
        try:
            await super().stream_response(send)
        finally:
            await close_async_iterator(self.body_iterator)


async def prepend_async_item(first_item: Any, iterator: AsyncIterator[Any]):
    """Yield a prefetched item before continuing the original iterator."""
    try:
        yield first_item
        async for item in iterator:
            yield item
    finally:
        await close_async_iterator(iterator)


async def read_first_async_item(iterator: AsyncIterator[Any]) -> Any:
    """Python 3.9-compatible async equivalent of built-in anext()."""
    return await iterator.__anext__()


async def build_streaming_response_or_error(
    iterator: AsyncIterator[Any],
    media_type: str = "text/event-stream",
):
    """
    Prefetch the first async item so router code can return an upstream error
    response directly before FastAPI commits a 200 streaming response.
    """
    try:
        first_item = await read_first_async_item(iterator)
    except StopAsyncIteration:
        return Response(status_code=204)

    if isinstance(first_item, Response):
        await close_async_iterator(iterator)
        return first_item

    return ManagedStreamingResponse(
        prepend_async_item(first_item, iterator),
        media_type=media_type,
    )
