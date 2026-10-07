# Bounded synthetic audit checks — 07/10/2026

Research evidence, not implementation or live-provider acceptance. Generator family: `openai`.
Run from the audit feature worktree. No credentials, real model requests, startup hooks,
probe loops or live service changes. The proxy check uses cached Nix Python dependencies;
the Desktop check imports only its installed session library. Network for the proxy
check is loopback only. The Desktop SSO function is replaced before calling it.
The Desktop wait is scaled from 60 s to 50 ms to avoid blocking the host.

Execute each Python fence below with its named interpreter (`-B`); stdout is the
receipt. `docs/evidence/GAP-AUDIT-2026-10-07-check-results.json` records the run.
These assertions deliberately pass when the audited gap reproduces. After a repair,
the reproducer must be replaced by the inverse regression assertions described in
the audit's acceptance section, not treated as a green repair test.

## Run both (use an admitted test budget)

```bash
python3 -B - <<'PY'
import pathlib, re, subprocess, sys
text = pathlib.Path('docs/evidence/GAP-AUDIT-2026-10-07-checks.md').read_text()
blocks = re.findall(r'```python\n(.*?)```', text, re.S)
interpreters = [
    '/nix/store/b5bpi6zfajzzrwwpgba2q6li3nnya4bs-python3-3.14.7/bin/python3.14',
    '/home/notroot/.local/share/mimo-desktop-api/.venv/bin/python',
]
assert len(blocks) == 2
results = [subprocess.run([exe, '-B', '-c', code], timeout=12).returncode
           for exe, code in zip(interpreters, blocks, strict=True)]
assert results == [0, 0], results
PY
```

## Proxy (`/nix/store/b5bpi6zfajzzrwwpgba2q6li3nnya4bs-python3-3.14.7/bin/python3.14 -B`)

```python
import ast
import asyncio
import json
import os
from pathlib import Path
import re
import site
import sys

wrapper = Path('/nix/store/jmak9s5hhv33bp53npypcnlkcrjn2cxz-anthropic-throttle-proxy-0.1.0/bin/.anthropic-throttle-proxy-wrapped')
paths = ast.literal_eval(re.search(r', (\[.*?\]), site\._init_pathinfo', wrapper.read_text()).group(1))
for path in paths:
    site.addsitedir(path)
sys.path.insert(0, str(Path('src').resolve()))
# Isolate from any caller's throttle configuration and disable real file custody.
for key in list(os.environ):
    if key.startswith('THROTTLE_'):
        del os.environ[key]
os.environ['THROTTLE_CREDENTIAL_STATE_FILE'] = ''
from aiohttp import ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer
from anthropic_throttle_proxy import config, forwarding, proxy, ratelimit

assert Path(forwarding.__file__).resolve().is_relative_to(Path('src').resolve())

async def main():
    sends = []
    async def upstream(request):
        sends.append(await request.read())
        if len(sends) == 1:
            # Provider accepted the entire POST; lose the reply BEFORE headers.
            request.transport.abort()
            return web.Response()
        return web.Response(text='second-send', headers={'Content-Type': 'text/plain'})
    upstream_app = web.Application()
    upstream_app.router.add_post('/v1/messages', upstream)
    async with TestServer(upstream_app) as provider:
        config.UPSTREAM = str(provider.make_url('')).rstrip('/')
        config.CENTRAL_URL = ''
        config.RATE_PUSHBACK_RETRIES = 0
        config.KEEPALIVE_HOLD = False
        attempt = proxy._Attempt()
        async def relay(request):
            body = await request.read()
            return await proxy._forward_with_retry(
                request=request, headers={'Authorization': 'Bearer synthetic'}, body=body,
                path='v1/messages', via='direct', url=config.UPSTREAM + '/v1/messages',
                client_timeout=ClientTimeout(total=3), attempt=attempt,
                bid='_anon', limiter=None,
            )
        app = web.Application()
        app.router.add_post('/v1/messages', relay)
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/v1/messages', data=b'{"model":"synthetic"}')
            await response.read()
            assert response.status == 200 and len(sends) == 2
            assert sends[0] == sends[1]
    # No content needs to be retained to parse the last usage event, but the
    # current tee keeps the PREFIX. Model exactly the cap in both pipe paths.
    start = b'data: {"type":"message_start","message":{"usage":{"input_tokens":11}}}\n\n'
    tail = b'data: {"type":"message_delta","usage":{"output_tokens":37}}\n\n'
    payload = start + b':' + b'x' * (1024 * 1024) + b'\n\n' + tail
    prefix_usage = ratelimit._parse_sse_usage(payload[:1024 * 1024])
    full_usage = ratelimit._parse_sse_usage(payload)
    assert prefix_usage['input'] == 11 and prefix_usage['output'] == 0
    assert full_usage['output'] == 37
    print(json.dumps({'uncertain_post_wire_sends':len(sends),
                      'uncertain_post_replayed_identically': sends[0] == sends[1],
                      'prefix_usage':prefix_usage, 'full_usage':full_usage,
                      'capture_scope':'synthetic cap/parser reproduction; not an actual live stream'}, sort_keys=True))
asyncio.run(main())
```

## Desktop (`/home/notroot/.local/share/mimo-desktop-api/.venv/bin/python -B`)

```python
import asyncio
import importlib.util
import json
from pathlib import Path
import threading
import time

path = Path('/home/notroot/.local/share/mimo-desktop-api/app/desktop_session.py')
spec = importlib.util.spec_from_file_location('gap_session', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
async def synthetic_sso(*args):
    await asyncio.sleep(0.005)
    return 'synthetic-cookie'
module._acquire_service_cookie = synthetic_sso
real_wait = threading.Event.wait
def bounded_wait(self, timeout=None):
    return real_wait(self, timeout=0.05)
module.threading.Event.wait = bounded_wait
async def main():
    credentials = {'mimoPassToken': 'synthetic-seed'}
    first = asyncio.create_task(module.get_service_cookie(credentials, client=object()))
    await asyncio.sleep(0)
    started = time.monotonic()
    second = await module.get_service_cookie(credentials, client=object())
    elapsed = time.monotonic() - started
    first_result = await first
    assert first_result == 'synthetic-cookie' and second is None
    assert elapsed >= 0.045
    print(json.dumps({'first_succeeded': first_result == 'synthetic-cookie',
                      'same_seed_second_returned_none': second is None,
                      'scaled_block_ms':round(elapsed*1000,2),
                      'scope':'synthetic SSO and 50ms wait; actual source timeout is 60s'}, sort_keys=True))
asyncio.run(main())
```
