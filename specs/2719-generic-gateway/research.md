# Decisions

- Reuse `proxy.main` and `ingress.main`. Config, routing, ingress registry and
  adapters already supply generic routing. Moving every implementation module
  would disturb dynamic imports, mutable singleton state and existing callers.
- Publish distribution `throttler-gateway` containing both `throttler_gateway`
  and `anthropic_throttle_proxy`. Retain legacy console aliases. Old distribution
  metadata lookup is not promised; consumers must install the canonical name.
  Do not co-install the old distribution: both own the compatibility package.
- Preserve existing state directories, environment variables, wire headers,
  root-probe bodies, metrics and build provenance. Root's migration owns future
  service/state transition; presentation branding does not authorize it.
- Retain the AIMD mark and Catppuccin palette. Update the wordmark, accessible
  titles and generic tagline; no redesign or raster asset is needed.
- Keep actual repository/registry URLs truthful. New-install Dokku examples
  use `throttler`; existing deployments retain their actual app/domain names.
- Validate installed entry-point dispatch without starting runtime collectors,
  opening listeners or making provider calls. Existing tests exercise the real
  HTTP, routing and security paths using their established fixtures.
