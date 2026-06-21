# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``reef.stubs.py_executor`` -- stub for the Python execution surface.

Drop-in replacement for ``coralbricks.sandbox.py_executor``. The hosted
runtime ships a sandboxed Python executor that runs model-emitted code
safely under resource limits and an import allowlist. The open-source
build exposes :class:`PyValidationError` (used by ``reef.tool`` for
exception-type matching) but does not provide an execution path -- the
:func:`reef.stubs.tools.py` stub is what raises on a real dispatch.
"""

from __future__ import annotations

_MSG = (
    "\n\n"
    "The Python executor verb (`run_python`) requires a sandboxed "
    "runtime to execute model-emitted code safely; this harness build "
    "does not ship one.\n\n"
    "To run locally, integrate RestrictedPython, a Docker / "
    "containerised executor, or another sandbox of your choice and "
    "replace this stub.\n"
)


class PyValidationError(Exception):
    """Raised when model-emitted Python fails the executor's static checks.

    In the open-source build this class exists for ``except`` clauses
    in :mod:`reef.tool` (and any consumer that re-raises it) but is
    not raised by any stub -- the kernel-verb stubs raise
    :class:`NotImplementedError` before the executor would have been
    reached.
    """


def execute(code: str, *args, **kwargs):
    """Stub for direct Python executor invocation.

    Not part of the ``cb_tools.py`` call path -- present for API parity
    only. Raises ``NotImplementedError`` with the redirect message.
    """
    raise NotImplementedError(_MSG)


# Hosted-runtime override. If the gateway's sandbox is on the path, re-import
# the real py_executor symbols so prod runs hit the real executor and the
# real :class:`PyValidationError` type (which consumer ``except`` clauses
# match on).
try:
    from coralbricks.sandbox.py_executor import *  # type: ignore[import-not-found]  # noqa: F401,F403
    _SANDBOX_AVAILABLE = True
except ImportError:
    _SANDBOX_AVAILABLE = False


__all__ = ["PyValidationError", "execute"]
