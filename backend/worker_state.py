"""Shared state for the background enrichment worker."""

# Depth, not a bool. A PSN sync chains follow-up jobs (store metadata, artwork
# fill, review art) that deliberately overlap, and each one paused the worker on
# entry and un-paused it in its `finally` — so the FIRST to finish resumed the
# enrichment and artwork-verification workers while the others were still
# writing. That is three writers on one SQLite connection, which is exactly the
# contention behind the post-import freeze. Counting means the workers only wake
# once the last job has actually finished.
#
# Reading/writing an int is safe under CPython's GIL; the increments happen from
# the single asyncio loop thread, so no lock is needed.
_pause_depth: int = 0


def pause_enrichment() -> None:
    global _pause_depth
    _pause_depth += 1


def resume_enrichment() -> None:
    """Release one pause. Clamped at zero so an unbalanced call can never leave
    the worker permanently paused."""
    global _pause_depth
    _pause_depth = max(0, _pause_depth - 1)


def is_paused() -> bool:
    return _pause_depth > 0
