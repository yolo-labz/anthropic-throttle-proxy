#!/usr/bin/env bash
# LD_LIBRARY_PATH for Playwright's bundled browsers on NixOS.
#
# Playwright ships its own Firefox and Chromium, and neither bundle has a
# dynamic-linker path to the host's X11/GTK stack: they abort at startup with
# `libgtk-3.so.0: cannot open shared object file` and `libglib-2.0.so.0: cannot
# open shared object file` respectively. The set cannot be hard-coded — it
# depends on which libraries each bundle happens to miss at this revision, and
# it CHANGES BETWEEN BROWSERS (Firefox needs the system-path GTK stack;
# Chromium additionally needs glib, which the system path does not carry).
#
# So this asks each binary what it is missing, resolves each soname in
# /nix/store, and repeats until that binary starts. The same loop a human runs
# by hand, which is why it converges in a couple of rounds.
#
# The resolved paths are version-locked store paths, so the answer changes
# after a `nixos-rebuild`; re-run this rather than caching its output.
#
# Usage:
#   LD_LIBRARY_PATH="$(specs/227-dashboard-audit/playwright-browser-libs.sh)" \
#     ~/.local/share/uv/tools/playwright/bin/python \
#       specs/227-dashboard-audit/browser-receipt.py
set -euo pipefail

CACHE="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"

bundles=()
# Firefox: the full bundle. Chromium: the headless shell the receipt drives.
for candidate in \
  "$CACHE"/firefox-*/firefox/firefox \
  "$CACHE"/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell; do
  [ -x "$candidate" ] && bundles+=("$candidate")
done

[ "${#bundles[@]}" -gt 0 ] || {
  echo "playwright-browser-libs: no Playwright browsers under $CACHE" >&2
  echo "  run: playwright install firefox chromium" >&2
  exit 1
}

# Sonames this binary still cannot resolve, from a failed start.
#
# The probe is EXPECTED to fail while libraries are missing, so its exit status
# is swallowed inside the braces: under `pipefail` the first element of a
# pipeline counts, and a 127 from the loader would abort this loop on the round
# it is supposed to be learning from.
missing_sonames() {
  local binary="$1" ld="$2"
  { LD_LIBRARY_PATH="$ld" "$binary" --version 2>&1 || true; } |
    grep -oE 'lib[A-Za-z0-9_.+-]+\.so(\.[0-9]+)*' |
    grep -vE '^lib(mozgtk|xul)\.so$' |
    sort -u
}

# ELF class of a file: 2 = 64-bit, 1 = 32-bit, 0 = unreadable. Byte 4 of the
# header is EI_CLASS. `od` rather than `file`, which is not in every closure.
elf_class() {
  [ -r "$1" ] || {
    echo 0
    return
  }
  od -An -tu1 -j4 -N1 "$1" 2>/dev/null | tr -d ' \n'
}

# The first store path for `soname` whose ELF class matches the CONSUMER's.
#
# `ls | head -1` is not good enough: /nix/store holds i686 builds of glib and
# gtk alongside the host's, and picking one stops the loop dead with
# `wrong ELF class: ELFCLASS32` — a failure that looks like a missing library
# and is really a mis-resolved one, so every further round resolves to the same
# wrong file and the loop never converges.
resolve_soname() {
  local soname="$1" want="$2" candidate
  for candidate in /nix/store/*/lib/"$soname"; do
    [ -e "$candidate" ] || continue
    if [ "$(elf_class "$candidate")" = "$want" ]; then
      dirname "$candidate"
      return 0
    fi
  done
  return 1
}

paths=""
for binary in "${bundles[@]}"; do
  class="$(elf_class "$binary")"
  for _ in $(seq 1 25); do
    want="$(missing_sonames "$binary" "$paths" || true)"
    [ -n "$want" ] || break
    add=""
    while read -r soname; do
      [ -n "$soname" ] || continue
      found="$(resolve_soname "$soname" "$class" || true)"
      [ -n "$found" ] && add="$add$found"$'\n'
    done <<<"$want"
    new="$(printf '%s\n%s\n' "$paths" "$add" | tr ':' '\n' | grep -v '^$' | sort -u)"
    [ -n "$new" ] || break
    paths="$(printf '%s' "$new" | paste -sd:)"
  done
done

[ -n "$paths" ] || {
  echo "playwright-browser-libs: could not resolve the bundles' libraries" >&2
  exit 1
}
printf '%s' "$paths"
