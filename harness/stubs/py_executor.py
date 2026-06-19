# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``harness.stubs.py_executor`` -- stub for the Python execution surface.

Drop-in replacement for ``coralbricks.sandbox.py_executor``. The hosted
runtime ships a sandboxed Python executor that runs model-emitted code
safely under resource limits and an import allowlist. The open-source
build exposes :class:`PyValidationError` (used by ``harness.tool`` for
exception-type matching) but does not provide an execution path -- the
:func:`harness.stubs.tools.py` stub is what raises on a real dispatch.
"""

from __future__ import annotations

_MSG = (
    "\n\n"
    "AlphaCumen's Python executor (`run_python`) requires a sandboxed "
    "runtime to execute model-emitted code safely.\n\n"
    "👉 For the hosted experience, talk to the Coral Bricks team:\n"
    "   https://coralbricks.ai/alphacumen\n\n"
    "👉 To run locally, integrate RestrictedPython, a Docker / "
    "containerised executor, or another sandbox of your choice.\n"
)


class PyValidationError(Exception):
    """Raised when model-emitted Python fails the executor's static checks.

    In the open-source build this class exists for ``except`` clauses in
    ``harness.tool`` and ``alphacumen.tools`` but is not raised by any
    stub -- the kernel-verb stubs raise :class:`NotImplementedError`
    before the executor would have been reached.
    """


def execute(code: str, *args, **kwargs):
    """Stub for direct Python executor invocation.

    Not part of the ``cb_tools.py`` call path -- present for API parity
    only. Raises ``NotImplementedError`` with the redirect message.
    """
    raise NotImplementedError(_MSG)


__all__ = ["PyValidationError", "execute"]
