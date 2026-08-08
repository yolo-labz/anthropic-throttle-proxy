"""anthropic-throttle-proxy — self-hosted throttle proxy for api.anthropic.com.

See README + CLAUDE.md for design + deploy.
"""

__version__ = "0.1.0"

# Directory this module was imported from. On the deploy hosts that is a
# /nix/store path, which makes it the running build's identity: `systemctl cat`
# says what the NEXT start will execute, `systemctl show` what the CURRENT one
# does, and neither answers "is the code in memory the code I merged". Three
# incidents in this repo's ledger are that same shape (the #29 root-probe stale
# unit, the #1681 stale fixed-output hash, and 08/08 when a pin was activated
# but the service never restarted), so health publishes it.
__build__ = str(__spec__.submodule_search_locations[0]) if __spec__ else ""
