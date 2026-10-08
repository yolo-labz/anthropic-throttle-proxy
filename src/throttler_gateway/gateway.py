"""Canonical gateway entry point; reuse the existing runtime and state."""

from anthropic_throttle_proxy.proxy import main

__all__ = ["main"]

if __name__ == "__main__":
    main()
