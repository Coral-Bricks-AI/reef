# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Shared plumbing for the AlphaCumen benchmark runners.

This package is intentionally narrow: it holds only what is shared by
both ``valsai/`` and ``financebench/`` -- the hosted-platform HTTP
client (``runner.py``), the Anthropic judge transport (``judge.py``),
small row-shaping helpers (``util.py``), and shared argparse fragments
(``cli.py``).
"""
