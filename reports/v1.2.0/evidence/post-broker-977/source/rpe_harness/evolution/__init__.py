"""Local evidence-backed evolution; proposals cannot approve their own oracle."""
from .store import EvolutionStore
from .cycle import cycle, record_verified_feedback
from .candidates import (propose_candidate, validate_candidate, apply_candidate, rollback,
                         prepare_candidate, quarantine_candidate)

__all__ = ['EvolutionStore', 'cycle', 'record_verified_feedback', 'propose_candidate',
           'validate_candidate', 'apply_candidate', 'rollback', 'prepare_candidate',
           'quarantine_candidate']
