"""Private worker bootstrap for a plugin selected by the trusted host registry.

Serialized Python code is never accepted from proposals, MCP, or event payloads.
ProposalStore alone writes this short-lived transport in its private worker
directory, pins its digest, and launches the usual CLI execution pipeline.
"""
from __future__ import annotations

import hashlib
import pickle
import sys

from .cli import main
from .contract_sdk import DEFAULT_REGISTRY
from .path_binding import inherited_binding, read_regular_file


def run():
    plugin_path, expected_digest, *arguments = sys.argv[1:]
    raw = read_regular_file(plugin_path, max_bytes=16 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError('registered plugin transport changed before launch')
    DEFAULT_REGISTRY.register(pickle.loads(raw), replace=True)
    with inherited_binding():
        return main(arguments)


if __name__ == '__main__':
    raise SystemExit(run())
