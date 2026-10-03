"""In-process coordination for the human application-approval checkpoint."""

import asyncio
from typing import Dict


class ReviewCoordinator:
    """Connect dashboard decisions to the browser task waiting for approval."""

    def __init__(self) -> None:
        self._waiters: Dict[int, asyncio.Future[str]] = {}

    def create_waiter(self, review_id: int) -> asyncio.Future[str]:
        """Register a review before it is announced to dashboard clients."""
        existing = self._waiters.get(review_id)
        if existing is not None and not existing.done():
            return existing
        waiter = asyncio.get_running_loop().create_future()
        self._waiters[review_id] = waiter
        return waiter

    def decide(self, review_id: int, decision: str) -> bool:
        """Resolve an active review with either ``approved`` or ``rejected``."""
        if decision not in {"approved", "rejected"}:
            raise ValueError(f"Unsupported review decision: {decision}")
        waiter = self._waiters.get(review_id)
        if waiter is None or waiter.done():
            return False
        waiter.set_result(decision)
        return True

    def finish(self, review_id: int) -> None:
        """Forget a completed, rejected, or expired review."""
        self._waiters.pop(review_id, None)

    def is_active(self, review_id: int) -> bool:
        waiter = self._waiters.get(review_id)
        return waiter is not None and not waiter.done()


review_coordinator = ReviewCoordinator()
