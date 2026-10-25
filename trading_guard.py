"""Task-reentrant serialization for workflows on the shared exchange account.

Executor work is awaited to completion even if the caller is cancelled. This
protects only this process, not manual trades or another deployment/account bot.
"""
import asyncio
from functools import wraps


class TradingGuard:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner = None

    def serialized(self, function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            if self._owner is asyncio.current_task():
                return await function(*args, **kwargs)
            async with self._lock:
                async def run():
                    self._owner = asyncio.current_task()
                    try:
                        return await function(*args, **kwargs)
                    finally:
                        self._owner = None

                worker = asyncio.create_task(run())
                cancelled = False
                while True:
                    try:
                        result = await asyncio.shield(worker)
                        break
                    except asyncio.CancelledError:
                        if worker.cancelled():
                            raise
                        # Cancelling run_in_executor does not stop its thread.
                        # Keep ownership until the entire workflow has settled.
                        cancelled = True
                    except Exception:
                        if cancelled:
                            raise asyncio.CancelledError from None
                        raise
                if cancelled:
                    raise asyncio.CancelledError
                return result
        return wrapped


trading_guard = TradingGuard()
