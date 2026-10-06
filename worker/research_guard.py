"""Request-boundary cancellation scoped to the coordinator's research context."""
from contextlib import contextmanager
from contextvars import ContextVar

from .core import Review


_continue_research = ContextVar('office_continue_research', default=None)


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
