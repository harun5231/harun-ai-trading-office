"""Project authenticated native market facts into the existing browser prompt.

The caller pins the original module and installs this hook during image build.
The original prompt, output schema, operation marker and formatter are retained.
Only the serialized public market context is projected to exactly 200 CLOSED
candles per timeframe, with running candles clearly separated. There is no
browser, model, account, risk, order or network operation here.
"""
from __future__ import annotations

import copy
import json
from types import ModuleType
import time

from .native_market_context import convert_native_market_context


MAX_NATIVE_CONTEXT = 2 * 1024 * 1024
MAX_PROMPT_BYTES = 384 * 1024


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def install(native_namespace, *, expected_prompt, context_key, wall_clock=time.time):
    """Wrap a source-pinned formatter; never infer a private payload field name.

    ``context_key`` must be read from the authenticated native formatter's AST.
    Namespace/source authentication and deployment remain the caller's duty.
    """
    native = native_namespace.__dict__ if isinstance(native_namespace, ModuleType) else native_namespace
    if (type(native) is not dict or not callable(expected_prompt)
            or native.get('user_prompt') is not expected_prompt
            or type(context_key) is not str or context_key != 'context'
            or not callable(wall_clock)):
        raise ValueError('NATIVE_200_PROMPT_CONFIGURATION_UNVERIFIED')

    def user_prompt(payload):
        if type(payload) is not dict or context_key not in payload:
            raise ValueError('NATIVE_200_PROMPT_PAYLOAD_UNVERIFIED')
        # Screening has no selected symbol or chart context. Preserve that
        # existing path byte for byte; the native schema/gate still admits it.
        if payload[context_key] is None:
            return expected_prompt(payload)
        before = _canonical(payload)
        if len(before) > MAX_NATIVE_CONTEXT:
            raise ValueError('NATIVE_200_PROMPT_PAYLOAD_LIMIT')
        context = convert_native_market_context(payload[context_key], now=wall_clock())
        projected = copy.deepcopy(payload)
        # The authenticated native encoder recursively iterates dict.items();
        # it does not sort keys. Arrange only this new public projection in
        # canonical order so the unchanged encoder emits all 400 facts in the
        # exact canonical form that the admission check below verifies.
        projected[context_key] = json.loads(_canonical(context))
        formatted = expected_prompt(projected)
        if (type(formatted) is not str or not formatted
                or len(formatted.encode('utf-8')) > MAX_PROMPT_BYTES
                or _canonical(payload) != before):
            raise ValueError('NATIVE_200_PROMPT_RESULT_UNVERIFIED')
        # Reject a formatter that silently drops or truncates context. The
        # original JSON encoder may use either ASCII or UTF-8 escaping; public
        # OHLCV field names and decimal strings are ASCII in both cases.
        serialized = json.dumps(context, sort_keys=True, separators=(',', ':'),
                                ensure_ascii=False, allow_nan=False)
        if serialized not in formatted:
            raise ValueError('NATIVE_200_PROMPT_CONTEXT_NOT_SERIALIZED')
        return formatted

    user_prompt.__name__ = 'user_prompt'
    user_prompt.__harun_closed_candles_per_timeframe__ = 200
    native['user_prompt'] = user_prompt
    return user_prompt
