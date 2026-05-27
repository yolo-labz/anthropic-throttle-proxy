---
name: nix-user-service
description: Use when verifying or repairing the desktop Nix/Home Manager user service for anthropic-throttle-proxy, especially stale systemd unit paths, runtime drop-ins, daemon-reload behavior, or post-restart persistence.
---

# Nix User Service

## Evidence Checklist

Run these before editing Nix or restarting:

```sh
systemctl --user cat anthropic-throttle-proxy.service
systemctl --user show anthropic-throttle-proxy.service \
  -p ExecStart -p FragmentPath -p DropInPaths -p ActiveState -p SubState
readlink -f ~/.config/systemd/user/anthropic-throttle-proxy.service
readlink -f ~/.local/state/nix/profiles/home-manager
ls -la /run/user/1000/systemd/user/anthropic-throttle-proxy.service.d/ 2>/dev/null
```

## Verification Rules

- `cat` shows persisted unit files; `show` shows effective runtime.
- Runtime drop-ins under `/run/user/1000/systemd/user/` can override a stale persisted unit.
- Persistence is fixed only after removing or superseding runtime overrides, `daemon-reload`, restart, and re-checking both `cat` and `show`.
- For this incident, the fixed desktop binary was the `mg70...anthropic-throttle-proxy-0.1.0` store path. Do not regress to the older pre-root-probe path.

## Safe Restart Sequence

Only restart when the user service is expected to tolerate a short interruption.

```sh
systemctl --user daemon-reload
systemctl --user restart anthropic-throttle-proxy.service
systemctl --user show anthropic-throttle-proxy.service -p ExecStart -p ActiveState -p SubState
curl -fsS http://127.0.0.1:8765/__throttle/health | jq
```

If the service fails, inspect `journalctl --user -u anthropic-throttle-proxy.service -n 120 --no-pager` before changing anything else.

## niri-guard activation gap (desktop host)

`nh os switch` on the desktop host is rewritten to `nh os boot` by a niri-guard wrapper — it stages the new system but does **not** run `home-manager.activationPackage`. So a fresh Nix store contains the correct HM-files derivation, but the live symlink at `~/.config/systemd/user/<unit>.service` keeps pointing at the previous HM gen until a logout/reboot or manual HM activation.

Surgical workaround (no reboot, no logout) — only when the canonical HM-files unit is verified correct:

```sh
TOPLEVEL=$(readlink /run/current-system)
HM_FILES=$(nix-store -qR "$TOPLEVEL" | grep -E 'home-manager-files$' | head -1)
NEW_UNIT="$HM_FILES/.config/systemd/user/anthropic-throttle-proxy.service"
grep ExecStart "$NEW_UNIT"   # eyeball before swapping
ln -sfn "$NEW_UNIT" ~/.config/systemd/user/anthropic-throttle-proxy.service
systemctl --user daemon-reload
/run/current-system/sw/bin/rm -f \
  /run/user/$(id -u)/systemd/user/anthropic-throttle-proxy.service.d/override.conf
systemctl --user restart anthropic-throttle-proxy.service
systemctl --user show anthropic-throttle-proxy.service -p ExecStart --value
```

After the swap: the symlink is durable across `daemon-reload` and `restart`, and the next real HM activation (logout/reboot or explicit `home-manager switch`) will rewrite it idempotently to the same path.

## Anti-patterns

- Declaring "fixed" from `systemctl --user show` alone — `show` is masked by transient drop-ins. Always compare to `cat`.
- `systemctl --user edit anthropic-throttle-proxy.service` to "patch" a stale unit — that creates **another** persistent drop-in that Home Manager will not own. Fix the source or swap the symlink instead.
- Restarting before verifying the new symlink target — you can pin a stale binary in memory and lose the evidence trail.
- `systemctl --user reset-failed` without first reading the journal — masks the underlying cause.
