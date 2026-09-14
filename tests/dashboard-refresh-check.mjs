import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// HTMX-only invariant (CLAUDE.md): the dashboard must render without
// JavaScript modules — exactly ONE script tag (the HTMX runtime), no inline
// watchdog code, no hx-on handlers. Staleness signalling is server-side:
// the swapped panel carries its own render-time stamp, and asset-versioned
// URLs give each build its own cache entries.
const html = readFileSync(new URL('../src/anthropic_throttle_proxy/ui/templates/dashboard.html', import.meta.url), 'utf8');
const stats = readFileSync(new URL('../src/anthropic_throttle_proxy/ui/templates/partials/stats.html', import.meta.url), 'utf8');

const scriptTags = html.match(/<script\b/g) ?? [];
assert.equal(scriptTags.length, 1, `expected exactly one <script> tag (HTMX), found ${scriptTags.length}`);
assert.match(html, /unpkg\.com\/htmx\.org/, 'the single script tag must be the HTMX runtime');
assert.doesNotMatch(html, /hx-on/, 'no inline htmx event handlers');
assert.doesNotMatch(html, /<noscript>/, 'no fallback scripting');

// The swapped panel must self-describe its freshness: a frozen "as of"
// stamp is the visible disconnect signal (replaces the removed JS watchdog).
assert.match(stats, /as of \{\{ as_of/, 'stats panel must render its own as-of stamp');
assert.match(stats, /data-revision="\{\{ asset_v/, 'stats panel must carry the build revision meta');
assert.match(stats, /data-local=/, 'stats panel must carry the local/sibling view flag');

console.log('PASS single-script invariant + server-side freshness stamp');
