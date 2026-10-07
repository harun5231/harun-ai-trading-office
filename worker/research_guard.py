"""Request-boundary cancellation scoped to the coordinator's research context."""
from contextlib import contextmanager
from contextvars import ContextVar

from .core import Review


_continue_research = ContextVar('office_continue_research', default=None)
_continue_gateway_mutation = ContextVar('office_continue_gateway_mutation', default=None)


class ResearchReadPaused(Review):
    """No new research read was started; an existing request may still finish."""


def check_research_read():
    continue_if = _continue_research.get()
    if continue_if is not None and not continue_if():
        raise ResearchReadPaused('ROBOT_RESEARCH_PAUSED')


@contextmanager
def research_reads(continue_if):
    """Restrict reads in this context without affecting the account poll thread."""
    if not callable(continue_if):
        raise TypeError('research read predicate must be callable')
    previous = _continue_research.get()
    predicate = continue_if if previous is None else lambda: previous() and continue_if()
    token = _continue_research.set(predicate)
    try:
        yield
    finally:
        _continue_research.reset(token)


@contextmanager
def gateway_transaction_reads():
    """Let an already-started adapter transaction finish its protection reads.

    The caller must check whether the callback may start before entering this
    scope. This only prevents a later OFF change from interrupting that same
    callback; normal research guards are restored before any next operation.
    """
    token = _continue_research.set(None)
    try:
        yield
    finally:
        _continue_research.reset(token)


def gateway_mutations_allowed():
    """An explicitly authorized live actor must still be ON at the wire boundary."""
    continue_if = _continue_gateway_mutation.get()
    if continue_if is None:
        return False
    try:
        return continue_if() is True
    except Exception:
        return False


@contextmanager
def gateway_mutations(continue_if):
    """Fence financial writes separately from reads that may finish after OFF.

    A nested scope cannot broaden an outer actor's permission. There is no
    implicit authorization for a standalone gateway or another thread.
    """
    if not callable(continue_if):
        raise TypeError('gateway mutation predicate must be callable')
    previous = _continue_gateway_mutation.get()
    predicate = continue_if if previous is None else lambda: previous() is True and continue_if() is True
    token = _continue_gateway_mutation.set(predicate)
    try:
        yield
    finally:
        _continue_gateway_mutation.reset(token)
