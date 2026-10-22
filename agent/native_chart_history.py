"""Read old, authenticated chart receipts without admitting old live requests.

Only the native ``source_evidence`` reader is wrapped. Its original DB reads,
request reconstruction, assistant/output hashes, marker and canonical equality
checks run again inside a separate globals dictionary on the legacy path. This
module never writes a receipt, changes CHART_SOURCE globally, fetches a chart or
calls a provider. The root release builder must pin both exact native functions
and supply the approved legacy source and UTC release cutoff.
"""
from __future__ import annotations

from datetime import datetime, timezone
import functools
import hashlib
import importlib
import inspect
import math
import re
from types import CodeType, FunctionType

from .native_pending_protocol import install_source_protocol


CURRENT_CHART_SOURCE = 'BINANCE_FUTURES_200_CLOSED_DATA_RENDER'
_INSTALLED = '_harun_native_chart_history'
_PENDING_INSTALLED = '_harun_pending_review_protocol'
_SHA = re.compile(r'[a-f0-9]{64}\Z', re.ASCII)
_UTC = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z', re.ASCII)
_MODULE = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\Z', re.ASCII)


def _need(condition):
    if not condition:
        raise ValueError('NATIVE_CHART_HISTORY_UNVERIFIED')


def _cutoff(value):
    _need(type(value) is str and _UTC.fullmatch(value) is not None)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        result = parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError('NATIVE_CHART_HISTORY_UNVERIFIED') from None
    _need(parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)
          and 0 < result <= 10**13 and math.isfinite(result))
    return result


def _native_function(value, namespace, name, arguments, expected_sha256):
    """Accept only an exact source-pinned, closure-free native function."""
    _need(type(expected_sha256) is str and _SHA.fullmatch(expected_sha256) is not None)
    try:
        original = inspect.unwrap(value)
        source = inspect.getsource(original)
        signature = inspect.signature(original, follow_wrapped=False)
    except (ValueError, TypeError, OSError, RecursionError):
        raise ValueError('NATIVE_CHART_HISTORY_UNVERIFIED') from None
    _need(type(original) is FunctionType and original.__globals__ is namespace
          and original.__closure__ is None and not original.__code__.co_freevars
          and original.__name__ == name and original.__module__ == namespace['__name__']
          and original.__defaults__ is None and original.__kwdefaults__ is None
          and tuple(signature.parameters) == arguments
          and all(parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                  and parameter.default is inspect.Parameter.empty
                  for parameter in signature.parameters.values())
          and 0 < len(source.encode()) <= 32768
          and hashlib.sha256(source.encode()).hexdigest() == expected_sha256)
    return original


def _nested_code(function, name):
    values = [value for value in function.__code__.co_consts
              if type(value) is CodeType and value.co_name == name]
    _need(len(values) == 1)
    return values[0]


def _cells(function):
    try:
        return dict(zip(function.__code__.co_freevars,
                        (cell.cell_contents for cell in function.__closure__ or ())))
    except (ValueError, TypeError):
        raise ValueError('NATIVE_CHART_HISTORY_UNVERIFIED') from None


def _pending_validator(value, original, namespace, review):
    """Never unwrap and silently drop another validator's extra policy."""
    _need(type(value) is FunctionType
          and value.__code__ is _nested_code(install_source_protocol, 'validate_chart_evidence')
          and value.__globals__ is install_source_protocol.__globals__
          and getattr(value, '__wrapped__', None) is original
          and getattr(value, _PENDING_INSTALLED, False) is True
          and value.__defaults__ is None and value.__kwdefaults__ is None)
    cells = _cells(value)
    _need(set(cells) == {'original', 'namespace', 'exception_type'}
          and cells['original'] is original and cells['namespace'] is namespace
          and cells['exception_type'] is review)


def _installed_reader(value, original, namespace, review, policy):
    _need(type(value) is FunctionType
          and value.__code__ is _nested_code(install_chart_history, 'source_evidence')
          and value.__globals__ is globals()
          and getattr(value, '__wrapped__', None) is original
          and getattr(value, _INSTALLED, None) == policy)
    cells = _cells(value)
    _need(set(cells) == {'evidence', 'legacy_evidence', 'review'}
          and cells['evidence'] is original and cells['review'] is review)
    legacy = cells['legacy_evidence']
    _need(type(legacy) is FunctionType and legacy.__code__ is original.__code__
          and legacy.__globals__ is not namespace and legacy.__closure__ is None
          and legacy.__globals__.get('CHART_SOURCE') == policy[0])


def install_chart_history(namespace, *, legacy_source, release_cutoff_utc,
                          source_evidence_sha256, validator_sha256):
    """Install one finite historical fallback, after the native pending protocol.

    Live validation keeps the new chart source. Only a native Review from the
    current historical reader can enter the legacy reader. The isolated legacy
    validator still requires both chart rows to carry the exact old source and
    the recorded request to have been created no later than the release cutoff.
    """
    _need(type(namespace) is dict and type(namespace.get('__name__')) is str
          and _MODULE.fullmatch(namespace['__name__']) is not None
          and namespace.get('CHART_SOURCE') == CURRENT_CHART_SOURCE
          and type(namespace.get('MAX_CHART_BYTES')) is int and namespace['MAX_CHART_BYTES'] > 0
          and type(legacy_source) is str and 1 <= len(legacy_source) <= 128
          and legacy_source.isascii() and all(32 <= ord(char) <= 126 for char in legacy_source)
          and legacy_source != CURRENT_CHART_SOURCE)
    cutoff = _cutoff(release_cutoff_utc)
    review = namespace.get('Review')
    _need(isinstance(review, type) and issubclass(review, Exception))
    policy = (legacy_source, release_cutoff_utc, source_evidence_sha256, validator_sha256)
    current = namespace.get('source_evidence')
    installed = getattr(current, _INSTALLED, None)
    evidence = _native_function(current, namespace, 'source_evidence', ('db', 'operation'),
                                source_evidence_sha256)
    validator = _native_function(namespace.get('validate_chart_evidence'), namespace,
        'validate_chart_evidence', ('value', 'schema', 'context', 'created', 'received_at', 'max_age'),
        validator_sha256)
    _pending_validator(namespace.get('validate_chart_evidence'), validator, namespace, review)
    # Reapplying the public pending installer to a private namespace must not
    # first mutate a native module. The existing release already installed it.
    try:
        api = importlib.import_module(namespace['__name__'].split('.')[0] + '.neuroapi')
    except (ImportError, ValueError):
        raise ValueError('NATIVE_CHART_HISTORY_UNVERIFIED') from None
    _need(getattr(getattr(api, 'canonical_output', None), _PENDING_INSTALLED, False) is True)
    if installed is not None:
        _installed_reader(current, evidence, namespace, review, policy)
        return
    # No pre-existing source-reader wrapper is silently discarded by cloning.
    _need(current is evidence)

    legacy_namespace = dict(namespace)
    legacy_namespace['CHART_SOURCE'] = legacy_source
    legacy_namespace['validate_chart_evidence'] = FunctionType(validator.__code__, legacy_namespace,
        name=validator.__name__, argdefs=None, closure=None)
    legacy_evidence = FunctionType(evidence.__code__, legacy_namespace,
        name=evidence.__name__, argdefs=None, closure=None)
    legacy_namespace['source_evidence'] = legacy_evidence
    install_source_protocol(legacy_namespace)
    legacy_validation = legacy_namespace['validate_chart_evidence']

    @functools.wraps(legacy_validation)
    def bounded_legacy_charts(value, schema, context, created, received_at, max_age):
        if (type(created) not in (int, float) or not 0 < created <= cutoff or not math.isfinite(created)
                or type(value) is not list or len(value) != 2
                or any(type(row) is not dict or row.get('source') != legacy_source for row in value)):
            raise review('NATIVE_LEGACY_CHART_HISTORY_UNVERIFIED')
        return legacy_validation(value, schema, context, created, received_at, max_age)

    legacy_namespace['validate_chart_evidence'] = bounded_legacy_charts

    @functools.wraps(evidence)
    def source_evidence(db, operation):
        try:
            return evidence(db, operation)
        except review as current_error:
            try:
                return legacy_evidence(db, operation)
            except review:
                raise current_error from None

    setattr(source_evidence, _INSTALLED, policy)
    namespace['source_evidence'] = source_evidence
