"""Shared state for the background enrichment worker."""

# Set to True while a library sync is running to pause the enrichment worker.
# Reading/writing a bool is safe under CPython's GIL; no lock needed here.
enrichment_paused: bool = False
