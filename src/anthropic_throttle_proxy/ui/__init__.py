"""HTMX dashboard + optional Haiku advisor.

Mounted by `proxy.main()` via `attach_ui(app)`. Keep this module dependency-
free from the hot path — the proxy must work even if the UI fails to import.
"""
