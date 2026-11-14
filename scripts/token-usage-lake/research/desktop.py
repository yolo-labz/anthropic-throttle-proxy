"""Bounded desktop CPU cluster. Both scheduler and workers require mutual TLS."""

import asyncio
import os
from pathlib import Path

from distributed import LocalCluster, Security


async def main():
    root = Path(os.environ["SCIENCE_TLS"])
    security = Security(
        tls_ca_file=str(root / "ca.crt"),
        tls_scheduler_cert=str(root / "server.crt"),
        tls_scheduler_key=str(root / "server.key"),
        tls_worker_cert=str(root / "server.crt"),
        tls_worker_key=str(root / "server.key"),
        tls_client_cert=str(root / "server.crt"),
        tls_client_key=str(root / "server.key"),
        require_encryption=True,
    )
    async with LocalCluster(
        host=os.environ["SCIENCE_BIND"],
        scheduler_port=18786,
        n_workers=1,
        threads_per_worker=2,
        memory_limit="3GiB",
        dashboard_address=None,
        security=security,
        protocol="tls",
        asynchronous=True,
    ) as cluster:
        print(cluster.scheduler_address, flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
