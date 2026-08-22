"""X/Twitter client — post probes and collect replies.

Ported and simplified from TAP's ``x_client.py`` (Aware repo):
- ``post_probe`` posts a tweet that always mentions the target
- ``poll_replies`` finds tweets mentioning our bot (i.e. target replies)
- Rate limiting handled by the transport (tweepy ``wait_on_rate_limit``)

The transport is injectable so tests can substitute a fake tweepy client
without network access.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from locus.config import LocusConfig
from locus.exceptions import TwitterError

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds


class TargetClient:
    """X API client for posting probes and polling replies."""

    def __init__(
        self,
        config: LocusConfig,
        transport: Optional[Any] = None,
    ) -> None:
        self.config = config
        self._transport = transport  # injectable tweepy.Client-compatible object
        self._our_user_id: Optional[str] = None

    def _get_transport(self):
        if self._transport is not None:
            return self._transport
        if self._transport is None:
            import tweepy

            self._transport = tweepy.Client(
                bearer_token=(
                    self.config.x_bearer_token.get_secret_value()
                    if self.config.x_bearer_token
                    else ""
                ),
                consumer_key=(
                    self.config.x_consumer_key.get_secret_value()
                    if self.config.x_consumer_key
                    else ""
                ),
                consumer_secret=(
                    self.config.x_consumer_secret.get_secret_value()
                    if self.config.x_consumer_secret
                    else ""
                ),
                access_token=(
                    self.config.x_access_token.get_secret_value()
                    if self.config.x_access_token
                    else ""
                ),
                access_token_secret=(
                    self.config.x_access_token_secret.get_secret_value()
                    if self.config.x_access_token_secret
                    else ""
                ),
                wait_on_rate_limit=True,
            )
        return self._transport

    # ── Posting ───────────────────────────────────────────────

    async def post_probe(self, text: str) -> str:
        """Post a probe tweet that always mentions the target.

        Returns:
            The ID of the posted tweet as a string.
        """
        text = self._ensure_target_mention(text)
        try:
            response = await self._retry(
                lambda: self._get_transport().create_tweet(text=text)
            )
        except Exception as e:
            raise TwitterError(f"Failed to post probe: {e}", original=e) from e
        return str(response.data["id"])

    def _ensure_target_mention(self, text: str) -> str:
        """Guarantee the target handle appears in the tweet text."""
        target = self.config.target_handle
        if target.lower() not in text.lower():
            text = f"{text} {target}"
        return text

    # ── Collecting ────────────────────────────────────────────

    async def poll_replies(self, since_id: Optional[str] = None) -> List[dict]:
        """Fetch tweets mentioning our handle (target replies to our probes).

        Paga oltre il limite di ``max_results`` seguendo il ``next_token`` nel
        ``meta`` della risposta, così le reply oltre i 100 del primo batch non
        vanno perse; i duplicati tra pagine consecutive vengono eliminati.

        Returns:
            List of reply dicts with keys: id, text, author_id, created_at,
            in_reply_to_tweet_id.
        """
        user_id = await self._resolve_our_user_id()
        if not user_id:
            return []
        replies: List[dict] = []
        seen: set = set()
        pagination_token: Optional[str] = None
        try:
            for _ in range(self.config.max_poll_pages):
                kwargs: dict = {
                    "id": user_id,
                    "since_id": since_id,
                    "max_results": self.config.max_poll_results,
                    "tweet_fields": [
                        "created_at",
                        "in_reply_to_user_id",
                        "referenced_tweets",
                    ],
                    "expansions": ["referenced_tweets.id"],
                }
                if pagination_token is not None:
                    kwargs["pagination_token"] = pagination_token
                response = await self._retry(
                    lambda kwargs=kwargs: self._get_transport().get_users_mentions(
                        **kwargs
                    )
                )
                if response.data:
                    for tweet_data in response.data:
                        rid = str(tweet_data.id)
                        if rid in seen:
                            continue
                        seen.add(rid)
                        replies.append(
                            {
                                "id": rid,
                                "text": tweet_data.text,
                                "author_id": str(tweet_data.author_id)
                                if hasattr(tweet_data, "author_id")
                                else "",
                                "created_at": (
                                    tweet_data.created_at
                                    or datetime.now(timezone.utc)
                                ).isoformat(),
                                "in_reply_to_tweet_id": self._get_reply_to_id(
                                    tweet_data
                                ),
                            }
                        )
                # Segue il cursore di paginazione finché esiste.
                meta = getattr(response, "meta", None) or {}
                pagination_token = meta.get("next_token")
                if not pagination_token:
                    break
        except Exception as e:
            raise TwitterError(f"Failed to poll mentions: {e}", original=e) from e
        return replies

    async def _resolve_our_user_id(self) -> Optional[str]:
        if self._our_user_id:
            return self._our_user_id
        handle = self.config.our_bot_handle.lstrip("@")
        if not handle:
            raise TwitterError("our_bot_handle not configured — cannot poll replies")
        try:
            response = await self._retry(
                lambda: self._get_transport().get_user(username=handle)
            )
            if response.data:
                self._our_user_id = str(response.data.id)
        except Exception as exc:
            # Non ingoiare l'errore: credenziali/handle sbagliati altrimenti
            # degradano a poll silenziosamente vuoti.
            logger.warning(
                "resolve_user_failed handle=%s error=%s", handle, exc
            )
            self._our_user_id = None
        return self._our_user_id

    @property
    def our_user_id(self) -> str:
        if self._our_user_id is None:
            raise TwitterError(
                "Our user ID not resolved — call _resolve_our_user_id() first"
            )
        return self._our_user_id

    # ── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _get_reply_to_id(tweet_data: Any) -> Optional[str]:
        if hasattr(tweet_data, "referenced_tweets") and tweet_data.referenced_tweets:
            for ref in tweet_data.referenced_tweets:
                if getattr(ref, "type", None) == "replied_to":
                    return str(ref.id)
        return None

    async def _retry(self, func, max_retries: int = MAX_RETRIES):
        """Execute a synchronous tweepy call off the event loop with backoff."""
        loop = asyncio.get_event_loop()
        last_error: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                return await loop.run_in_executor(None, func)
            except Exception as e:
                last_error = e
                wait_time = RETRY_BACKOFF_BASE ** (attempt + 1)
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait_time)
        raise TwitterError(
            f"Twitter API call failed after {max_retries} retries",
            original=last_error,
        )
