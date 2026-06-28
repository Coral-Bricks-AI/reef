"""Compatibility shim: ``ProgressLog`` / ``PhaseTicker`` live in :mod:`reef.progress`.

Kept here so existing run.py scripts (and the Architect-authored
templates that reference ``lib.progress.ProgressLog``) keep working
unchanged. New code should import from :mod:`reef.progress` directly.
"""

from reef.progress import PhaseTicker, ProgressLog

__all__ = ["PhaseTicker", "ProgressLog"]
