"""
Stream Throttler Hook for LiteLLM Proxy

Buffers upstream LLM tokens and streams them to the user at a constant
tokens-per-second rate. Enforces one concurrent stream per user to
prevent upstream token waste. Supports subscription expiration via the
built-in `duration` parameter.

Configuration via virtual key metadata:
    tps_limit: float  - tokens per second (default: 15)
    max_concurrent_streams: int  - max parallel streams per user (default: 1)

Subscription expiration (built-in):
    duration: "30d"  - key expires after 30 days
    duration: "7d"   - key expires after 7 days
    duration: "1d"   - key expires after 1 day (trial)

Example key generation (1 month subscription, 15 tokens/s):
    curl -X POST http://localhost:4000/key/generate \
      -H "Authorization: Bearer sk-master" \
      -H "Content-Type: application/json" \
      -d '{
        "metadata": {
          "user_id": "user_123",
          "tps_limit": 15
        },
        "tpm_limit": 900,
        "rpm_limit": 5,
        "duration": "30d",
        "max_budget": 20.0,
        "budget_duration": "30d"
      }'
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncGenerator, Dict, Optional

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.types.utils import ModelResponseStream

try:
    from litellm.proxy._types import UserAPIKeyAuth
except ImportError:
    UserAPIKeyAuth = Any  # type: ignore


class _PROXY_StreamThrottler(CustomLogger):
    """
    CustomLogger hook that:
    1. Buffers tokens from the upstream LLM provider (fast).
    2. Streams them to the client at a constant tokens-per-second rate.
    3. Blocks concurrent streaming requests per user (configurable).
    """

    def __init__(self):
        # user_id -> asyncio.Lock (held while a stream is active)
        self._active_streams: Dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_tps(self, user_api_key_dict: Any) -> float:
        """Read tps_limit from key metadata. Falls back to 15."""
        metadata = getattr(user_api_key_dict, "metadata", None) or {}
        if isinstance(metadata, str):
            import json
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        return float(metadata.get("tps_limit", 15.0))

    def _get_max_concurrent(self, user_api_key_dict: Any) -> int:
        """Read max_concurrent_streams from key metadata. Falls back to 1."""
        metadata = getattr(user_api_key_dict, "metadata", None) or {}
        if isinstance(metadata, str):
            import json
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        return int(metadata.get("max_concurrent_streams", 1))

    def _get_user_id(self, user_api_key_dict: Any) -> str:
        """Extract a stable user identifier from the key dict."""
        return (
            getattr(user_api_key_dict, "user_id", None)
            or getattr(user_api_key_dict, "key", None)
            or "anonymous"
        )

    def _estimate_tokens(self, chunk: ModelResponseStream) -> int:
        """
        Estimate how many tokens a chunk represents.
        Uses usage.completion_tokens if available, otherwise counts
        the text length / 4 as a rough estimate.
        """
        usage = getattr(chunk, "usage", None)
        if usage and getattr(usage, "completion_tokens", None):
            return int(usage.completion_tokens)

        # Rough heuristic: ~4 chars per token
        choices = getattr(chunk, "choices", [])
        if choices:
            delta = getattr(choices[0], "delta", None)
            if delta:
                content = getattr(delta, "content", None)
                if content and isinstance(content, str):
                    return max(1, len(content) // 4)
        return 1

    # ------------------------------------------------------------------
    # Pre-call hook: enforce 1 concurrent stream per user + expiry check
    # ------------------------------------------------------------------

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: Any,
        data: dict,
        call_type: str,
    ):
        """
        Before the LLM call:
        1. Check if the key is expired (duration-based subscription).
        2. Check if the user already has an active streaming request.
        """
        # --- EXPIRY CHECK ---
        # LiteLLM's built-in auth already checks `expires`, but we add a
        # friendlier error message for subscription expiry here.
        expires = getattr(user_api_key_dict, "expires", None)
        if expires is not None:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if isinstance(expires, str):
                expires = datetime.fromisoformat(expires)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < now:
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=401,
                    detail={
                        "error": {
                            "message": (
                                "Subscription expired. Your key expired at "
                                f"{expires.isoformat()}. Please renew your "
                                "subscription."
                            ),
                            "type": "subscription_expired",
                            "code": "key_expired",
                            "expires_at": expires.isoformat(),
                        }
                    },
                )

        # --- CONCURRENCY CHECK (streaming only) ---
        if not data.get("stream", False):
            return

        user_id = self._get_user_id(user_api_key_dict)
        max_concurrent = self._get_max_concurrent(user_api_key_dict)

        async with self._lock:
            active = self._active_streams.get(user_id)
            if active is not None and active.locked():
                if max_concurrent <= 1:
                    from fastapi import HTTPException
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "error": {
                                "message": (
                                    f"User {user_id} already has an active "
                                    "stream. Wait for it to finish before "
                                    "sending another request."
                                ),
                                "type": "throttler_error",
                                "code": "concurrent_stream_limit",
                            }
                        },
                    )

        verbose_proxy_logger.debug(
            "StreamThrottler: pre-call OK for user=%s", user_id
        )

    # ------------------------------------------------------------------
    # Post-call streaming iterator hook: buffer + throttle
    # ------------------------------------------------------------------

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        response: Any,
        request_data: dict,
    ) -> AsyncGenerator[ModelResponseStream, None]:
        """
        Wraps the upstream stream iterator. Buffers chunks from the
        provider and yields them at a constant tokens-per-second rate.
        """
        user_id = self._get_user_id(user_api_key_dict)
        tps = self._get_tps(user_api_key_dict)

        verbose_proxy_logger.info(
            "StreamThrottler: starting throttled stream for user=%s, tps=%.1f",
            user_id,
            tps,
        )

        # Acquire the per-user lock for the duration of the stream
        stream_lock = asyncio.Lock()
        async with self._lock:
            self._active_streams[user_id] = stream_lock

        try:
            async with stream_lock:
                await self._throttled_stream(user_id, tps, response)
        finally:
            async with self._lock:
                self._active_streams.pop(user_id, None)
            verbose_proxy_logger.info(
                "StreamThrottler: stream completed for user=%s", user_id
            )

    async def _throttled_stream(
        self,
        user_id: str,
        tps: float,
        upstream_response: Any,
    ) -> AsyncGenerator[ModelResponseStream, None]:
        """
        Core throttling logic. Drains upstream fast into a buffer,
        then yields chunks at a constant rate.
        """
        buffer: list[ModelResponseStream] = []
        upstream_done = False
        tokens_emitted = 0
        window_start = time.monotonic()

        # --- Drain upstream in background task ---
        async def _drain():
            nonlocal upstream_done
            try:
                async for chunk in upstream_response:
                    buffer.append(chunk)
            except Exception as e:
                verbose_proxy_logger.error(
                    "StreamThrottler: upstream error for user=%s: %s",
                    user_id,
                    str(e),
                )
                # Signal error via a special marker
                buffer.append(("error", e))
            finally:
                upstream_done = True

        drain_task = asyncio.create_task(_drain())

        try:
            while not upstream_done or buffer:
                if not buffer:
                    # Wait for upstream to produce more
                    await asyncio.sleep(0.005)
                    continue

                item = buffer.pop(0)

                # Handle upstream errors
                if isinstance(item, tuple) and item[0] == "error":
                    raise item[1]

                chunk: ModelResponseStream = item
                token_count = self._estimate_tokens(chunk)
                tokens_emitted += token_count

                # Calculate how long we should have waited by now
                elapsed = time.monotonic() - window_start
                expected_time = tokens_emitted / tps

                # If we're ahead of schedule, sleep to maintain constant rate
                sleep_duration = expected_time - elapsed
                if sleep_duration > 0:
                    await asyncio.sleep(sleep_duration)

                yield chunk

        finally:
            # Ensure drain task is cleaned up
            if not drain_task.done():
                drain_task.cancel()
                try:
                    await drain_task
                except asyncio.CancelledError:
                    pass

    # ------------------------------------------------------------------
    # Per-chunk hook (optional): log token counts for debugging
    # ------------------------------------------------------------------

    async def async_post_call_streaming_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        response: str,
    ) -> Any:
        """No-op per-chunk hook. Throttling is done at the iterator level."""
        return response
