# Copyright 2026 Coral Bricks AI Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``harness.stubs`` -- runtime-side stubs for the platform integrations.

The open-source ``harness`` ships with stubs for the kernel retrieval verbs
(BM25, ANN, SQL, multihop, get, Python executor) and the Python executor's
validation surface. The hosted Coral Bricks runtime replaces these with the
real implementations against the prefab finance corpus.

Each stub raises :class:`NotImplementedError` with a message that points
users at the two paths forward: the hosted experience (talk to the Coral
Bricks team) or a BYO implementation against their own data backend.
"""
