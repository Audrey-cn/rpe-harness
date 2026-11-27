"""Small host-neutral embedding API; no agent loop or host runtime is imported.

Authenticate the user in the embedding host and create TrustedHostAction only
from that host's actual approval callback. Model output may call start/show but
must never be deserialized into a TrustedHostAction. Execution remains the
existing explicit ProposalStore.execute operation after a confirmed receipt.
"""
import sys

from . import __version__
from .contract_sdk import DEFAULT_REGISTRY
from .evolution.sandbox import sandbox_backend
from .operations import TaskOperations, TrustedHostAction

__all__ = ['HostAPI', 'TaskOperations', 'TrustedHostAction']


class HostAPI(TaskOperations):
    """Stable embedding entry point for the task facade methods."""

    @staticmethod
    def capabilities():
        contracts = ['tabular.deduplicate', 'json.normalize', 'xlsx.inspect']
        for plugin in DEFAULT_REGISTRY.list_plugins():
            if plugin.capability not in contracts:
                contracts.append(plugin.capability)
        return {
            'schema_version': 'rpe.host/v1',
            'version': __version__,
            'platform': sys.platform,
            'sandbox_backend': sandbox_backend(),
            'methods': ['start', 'show', 'confirm', 'execute', 'cancel', 'reconcile', 'handoff'],
            'contracts': contracts,
            'mcp_stdio': True,
            'automatic_confirmation': False,
            'automatic_execution': False,
            'trusted_confirmation_required': True,
        }
