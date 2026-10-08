# Entry-point and migration contract

| Surface | Export / behavior |
|---|---|
| Distribution | `throttler-gateway==0.1.0` |
| Gateway command | `throttler-gateway` → `throttler_gateway.gateway:main` |
| Ingress command | `throttler-ingress` → `throttler_gateway.ingress:main` |
| Gateway module | `python -m throttler_gateway` |
| Ingress module | `python -m throttler_gateway.ingress` |
| Legacy commands | `anthropic-throttle-proxy`, `anthropic-throttle-ingress` |
| Legacy imports/modules | `anthropic_throttle_proxy` and existing submodules |
| Runtime | Same gateway/ingress `main`, settings and mutable state |
| Resources | Existing implementation package's templates and static assets |
| Compatibility | Existing env, state paths, headers, metrics and root probes |

Nix consumer must include both package directories when overriding the wheel
contents. No Python-version-specific site-packages path is introduced. Replace
the old distribution rather than co-installing it. Repository/registry/app/domain
names are external coordinates, not automatically migrated product aliases.
