"""Small host-neutral embedding API; no agent loop or host runtime is imported.

Authenticate the user in the embedding host and create TrustedHostAction only
from that host's actual approval callback. Model output may call start/show but
must never be deserialized into a TrustedHostAction. Execution remains the
existing explicit ProposalStore.execute operation after a confirmed receipt.
"""
import sys

from .evolution.sandbox import sandbox_backend
from .operations import TaskOperations, TrustedHostAction

__all__ = ['HostAPI', 'TaskOperations', 'TrustedHostAction']


class HostAPI(TaskOperations):
    """Stable embedding entry point for the task facade methods."""

    @staticmethod
    def capabilities():
        return {'schema_version': 'rpe.host/v1', 'version': '1.19.0',
                'platform': sys.platform,
                'sandbox_backend': sandbox_backend(),
                'methods': ['start', 'show', 'confirm', 'execute', 'cancel', 'reconcile', 'handoff'],
                'automatic_confirmation': False, 'automatic_execution': False,
                'trusted_confirmation_required': True}
