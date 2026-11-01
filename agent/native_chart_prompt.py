"""Project authenticated native market facts into the existing browser prompt.

The caller pins the original module and installs this hook during image build.
The internal prompt/schema/operation identity are retained; WEB receives natural text.
The WEB message names two CSV attachments containing exactly 200 CLOSED
candles per timeframe. Internal native context and risk provenance stay intact.
There is no browser, model, account, risk, order or network operation here.
"""
from __future__ import annotations

import copy
import json
from types import ModuleType
import time

from .native_market_context import convert_native_market_context
from .native_data_files import prepare_data_files, render_short_prompt, render_screening_prompt


MAX_NATIVE_CONTEXT = 2 * 1024 * 1024
MAX_PROMPT_BYTES = 384 * 1024


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def screening_payload(payload):
    """Recognize the native coin-selection schema, including replacements."""
    if type(payload) is not dict:
        return False
    schema = payload.get('output_schema')
    return type(schema) is dict and schema.get('required') == ['symbols']


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
        # Replacement screening carries exclusions, without selected-coin
        # candles. The native formatter retains that context byte for byte;
        # its existing schema/gate still owns admission.
        if screening_payload(payload):
            return render_screening_prompt(payload, expected_prompt(payload))
        if payload[context_key] is None:
            return expected_prompt(payload)
        before = _canonical(payload)
        if len(before) > MAX_NATIVE_CONTEXT:
            raise ValueError('NATIVE_200_PROMPT_PAYLOAD_LIMIT')
        context = convert_native_market_context(payload[context_key], now=wall_clock())
        files = prepare_data_files(context, now=wall_clock())
        projected = copy.deepcopy(payload)
        # Preserve the source-pinned owner prompt, schema and operation marker,
        # but let the native formatter emit an empty inline context. The exact
        # 400 CLOSED records are delivered as two independently bound files.
        projected[context_key] = None
        original = expected_prompt(projected)
        formatted = render_short_prompt(payload, original, files, now=wall_clock())
        if (type(formatted) is not str or not formatted
                or len(formatted.encode('utf-8')) > MAX_PROMPT_BYTES
                or _canonical(payload) != before):
            raise ValueError('NATIVE_200_PROMPT_RESULT_UNVERIFIED')
        return formatted

    user_prompt.__name__ = 'user_prompt'
    user_prompt.__harun_closed_candles_per_timeframe__ = 200
    user_prompt.__harun_data_files_v1__ = True
    native['user_prompt'] = user_prompt
    return user_prompt
