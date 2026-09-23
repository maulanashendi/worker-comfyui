import os

# tests/test_handler.py exercises the legacy upstream `_handle_job` path via
# handler.handler() without setting input.protocol; senai-worker/1 requires
# LEGACY_UPSTREAM_INPUT=true to opt into that path (contract §3.1). Default it
# on here so those pre-existing tests keep exercising the legacy path; tests
# that care about the senai-worker/1 dispatch set/delete this explicitly per
# test via monkeypatch, which overrides (and auto-restores) this default.
os.environ.setdefault("LEGACY_UPSTREAM_INPUT", "true")
