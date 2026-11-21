"""Run the descriptor-bound falsifier against the installed capture implementation."""

import importlib.util
import tempfile
from pathlib import Path


def main():
    path = Path.home() / ".local/share/token-science/acquire.py"
    spec = importlib.util.spec_from_file_location("installed_acquire", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("installed acquisition module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as temporary:
        store = module.Store(Path(temporary))
        try:
            for index in range(600):
                store.append(f"raw-restricted/fixture/{index}.jsonl", b"{}\n")
                if len(store.fh) > 128:
                    raise RuntimeError("descriptor cache exceeds 128")
            store.commit({}, {})
            if len(store.st["out"]) != 600:
                raise RuntimeError("committed output lengths lost")
        finally:
            store.close()
    print("PASS: 600 outputs, at most 128 open handles, committed lengths retained")


if __name__ == "__main__":
    main()
