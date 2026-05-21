"""Entry point: `python -m anthropic_throttle_proxy` boots the aiohttp app.

The proxy module's `main()` does all the wiring (lock binding, route
registration, UI mounting, web.run_app). Keep this file thin so the
re-entry surface stays simple.
"""

from .proxy import main

if __name__ == "__main__":
    main()
