"""Canonical ingress entry point; reuse the existing routing and policy."""

from anthropic_throttle_proxy.ingress import main

__all__ = ["main"]

if __name__ == "__main__":
    main()
