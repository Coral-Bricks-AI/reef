"""``polyp.runner`` -- per-task entrypoints that drive the cbq state machine.

Each phase (code/analyze/suggest, plus the per-side watcher loops) is
its own module with a ``main()`` function and ``__main__`` block so
systemd and ad-hoc shells can invoke it as
``python -m polyp.runner.<phase> <id>``.
"""
