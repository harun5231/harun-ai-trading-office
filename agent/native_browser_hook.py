"""Delegate clarification inside the existing native browser transaction.

The native actor, browser profile, first prompt, ROBOT dispatch gate, chart
capture, response identifier, receipt callback and worker/gateway remain the
execution authority. This wrapper never starts Chromium or a coordinator.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone

from .native_file_upload import NativeDataFileDelivery, enabled as data_file_mode


def _same_text(left, right):
    return (type(left) is str and type(right) is str
            and left.replace('\r\n', '\n').strip() == right.replace('\r\n', '\n').strip())


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode('ascii')


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


# This subset is verbatim source from the unchanged production Risk Manager.
# It defines only the existing TP calculation and its pure dependencies.
_NATIVE_TP_CORE_SHA256 = '2c694f4cb7916aa7205d79a26cb426ae36a9098196bfe26dd9faadc9b0fe2166'
_NATIVE_TP_SOURCE_SHA256 = '63c73a52da90a0a5e6cf7b9eda852329c69771ea43951f657f3de908eac290a6'
_NATIVE_TP_SOURCE = 'D = Decimal\n\nFEE_SOURCE = \'BINANCE_FUTURES_COMMISSION_RATE\'\n\nEXIT_SLIPPAGE_RATE = D(\'0.005\')\n\nclass Review(Exception): pass\n\ndef number(value):\n    if not isinstance(value, (str,int,Decimal)) or isinstance(value,bool):\n        raise Review(\'NEEDS_REVIEW: nilai numerik harus eksplisit\')\n    if isinstance(value,D):\n        if not value.is_finite():raise Review(\'NEEDS_REVIEW: angka harus positif\')\n        if abs(value.as_tuple().exponent)>64:raise Review(\'INVALID_NUMBER_SIZE\')\n        text=format(value,\'f\')\n    else:text=str(value).strip()\n    if len(text)>64:raise Review(\'INVALID_NUMBER_SIZE\')\n    if not re.fullmatch(r\'\\d+(?:\\.\\d+)?\',text):\n        raise Review(\'NEEDS_REVIEW: angka ambigu / rentang / pemisah ribuan\')\n    try: value = D(text)\n    except InvalidOperation: raise Review(\'NEEDS_REVIEW: angka tidak valid\')\n    if not value.is_finite() or value <= 0: raise Review(\'NEEDS_REVIEW: angka harus positif\')\n    return value\n\ndef _exact_decimal(value):\n    """Convert finite decimal fractions exactly, independently of Decimal context."""\n    value=Fraction(value);denominator=value.denominator;twos=fives=0\n    while denominator%2==0:denominator//=2;twos+=1\n    while denominator%5==0:denominator//=5;fives+=1\n    if denominator!=1:raise Review(\'INVALID_COST_MODEL\')\n    places=max(twos,fives)\n    coefficient=value.numerator*2**(places-twos)*5**(places-fives)\n    digits=str(abs(coefficient)).rjust(places+1,\'0\')\n    text=digits if not places else digits[:-places]+\'.\'+digits[-places:]\n    return D((\'-\' if coefficient<0 else \'\')+text)\n\ndef validated_fee_rate(rules,symbol=None,*,check_fresh=True):\n    """Require per-symbol account evidence; an absent fee is never assumed zero."""\n    if any(getattr(rules,key,None) is None for key in\n           (\'taker_fee_rate\',\'fee_observed_at\',\'fee_symbol\',\'fee_source\')):\n        raise Review(\'FEE_EVIDENCE_UNAVAILABLE\')\n    rate=rules.taker_fee_rate;observed=rules.fee_observed_at\n    if (rules.fee_source!=FEE_SOURCE or not isinstance(rules.fee_symbol,str)\n            or not re.fullmatch(r\'[A-Z0-9]{2,18}USDT\',rules.fee_symbol)\n            or symbol is not None and rules.fee_symbol!=symbol\n            or not isinstance(rate,D) or not rate.is_finite() or not 0<=rate<1\n            or abs(rate.as_tuple().exponent)>64 or len(format(rate,\'f\'))>64\n            or type(observed) not in (int,float) or not math.isfinite(observed) or observed<0):\n        raise Review(\'INVALID_FEE_EVIDENCE\')\n    if check_fresh and not 0<=time.time()-observed<=300:raise Review(\'STALE_FEE_EVIDENCE\')\n    return rate\n\ndef _adverse_exit_price(trigger,side):\n    if side not in (\'LONG\',\'SHORT\'):raise Review(\'INVALID_SIDE\')\n    factor=1-Fraction(EXIT_SLIPPAGE_RATE) if side==\'LONG\' else 1+Fraction(EXIT_SLIPPAGE_RATE)\n    return Fraction(trigger)*factor\n\ndef _reserve_unit_loss(entry,sl,side,fee):\n    if side not in (\'LONG\',\'SHORT\'):raise Review(\'INVALID_SIDE\')\n    if not (sl<entry if side==\'LONG\' else sl>entry):raise Review(\'INVALID_ENTRY_TP_SL\')\n    adverse=_adverse_exit_price(sl,side)\n    return abs(entry-sl)+sl*Fraction(EXIT_SLIPPAGE_RATE)+fee*(entry+adverse)\n\ndef normalized_reward_risk_tp(entry,stop_loss,side,rules,*,symbol=None,check_fresh=False):\n    """First legal trigger whose reserved adverse TP fill yields planned net 1:2."""\n    e,sl,tick=(Fraction(number(value)) for value in (entry,stop_loss,rules.tick))\n    fee=Fraction(validated_fee_rate(rules,symbol,check_fresh=check_fresh))\n    reserve=Fraction(EXIT_SLIPPAGE_RATE)\n    loss=_reserve_unit_loss(e,sl,side,fee)\n    target=(e*(1+fee)+2*loss)/((1-reserve)*(1-fee)) if side==\'LONG\' else (e*(1-fee)-2*loss)/((1+reserve)*(1+fee))\n    steps=target/tick\n    count=-(-steps.numerator//steps.denominator) if side==\'LONG\' else steps.numerator//steps.denominator\n    price=count*tick\n    if price<=0 or not (price>e if side==\'LONG\' else price<e):raise Review(\'INVALID_ENTRY_TP_SL\')\n    return _exact_decimal(price)\n'


class NativeTPConfirmationUnavailable(ValueError):
    pass


def _neurobro_tp_confirmation(output, context, *, now):
    """Ask Neurobro for the actual TP; never alter its returned setup.

    The existing core's exact calculation supplies a proposal only. A matching
    Neurobro TP or HOLD needs no extra turn. The model decision and original
    answer remain immutable; the caller owns any required clarification.
    """
    import hashlib
    import math
    import re
    from decimal import Decimal, InvalidOperation
    from fractions import Fraction
    from types import SimpleNamespace

    def need(value):
        if not value:
            raise NativeTPConfirmationUnavailable('NEUROBRO_TP_RULES_UNAVAILABLE')

    if type(output) is not dict or output.get('side') not in ('LONG', 'SHORT'):
        return None
    need(type(now) in (int, float) and math.isfinite(now) and now > 0)
    need(type(context) is dict)
    contract, risk = context.get('contract_rules'), context.get('risk_constraints')
    need(type(contract) is dict and type(risk) is dict)
    symbol, side = output.get('symbol'), output['side']
    need(type(symbol) is str and re.fullmatch(r'[A-Z0-9]{2,18}USDT', symbol)
        and context.get('symbol') == contract.get('symbol') == risk.get('fee_symbol') == symbol
        and contract.get('source') == 'Binance Futures /fapi/v1/exchangeInfo'
        and risk.get('fee_source') == 'BINANCE_FUTURES_COMMISSION_RATE'
        and risk.get('risk_model') == 'FEE_SLIPPAGE_RISK_V3'
        and risk.get('reward_risk_policy') == 'NET_1_TO_2_NORMALIZED_WITH_EXIT_RESERVE')
    observed, fee_observed = contract.get('observed_at'), risk.get('fee_observed_at')
    # The worker already authenticated this request's fee/rule snapshot. A
    # discussion must not invent a new expiry for that immutable snapshot;
    # the native operation deadline and final worker refresh remain decisive.
    need(all(type(v) in (int, float) and math.isfinite(v) and 0 < v <= now
        for v in (observed, fee_observed)))
    need(hashlib.sha256(_NATIVE_TP_SOURCE.encode()).hexdigest() == _NATIVE_TP_SOURCE_SHA256)
    namespace = {'__name__': 'unchanged_native_tp_risk_definitions', 'Decimal': Decimal, 'InvalidOperation': InvalidOperation,
        'Fraction': Fraction, 'math': math, 're': re, 'time': SimpleNamespace(time=lambda: now)}
    exec(compile(_NATIVE_TP_SOURCE, '<unchanged-production-tp-calculation>', 'exec'), namespace)

    def number(value, *, zero=False):
        # Native DOM JSON can contain floats; preserve its serialized decimal
        # value, never Decimal(binary_float). The worker then reads that text.
        need(type(value) in (str, int, float, Decimal) and not isinstance(value, bool))
        if type(value) is float:
            need(math.isfinite(value))
            value = str(value)
        try:
            parsed = Decimal(value)
            need(parsed.is_finite() and (parsed >= 0 if zero else parsed > 0) and abs(parsed.adjusted()) <= 63)
            if zero and parsed == 0:
                return parsed
            return namespace['number'](format(parsed, 'f'))
        except (InvalidOperation, namespace['Review']):
            raise NativeTPConfirmationUnavailable('NEUROBRO_TP_RULES_UNAVAILABLE') from None

    entry, current, stop = (number(output.get(k)) for k in ('limit_entry', 'take_profit', 'stop_loss'))
    tick, rate = number(contract.get('tickSize')), number(risk.get('entry_fee_rate'), zero=True)
    need(Decimal('0') <= rate < Decimal('1'))
    need(all(number(risk.get(k), zero=True) == rate for k in ('sl_exit_fee_rate', 'tp_exit_fee_rate')))
    need(number(risk.get('exit_slippage_rate')) == namespace['EXIT_SLIPPAGE_RATE'])
    need(stop < entry < current if side == 'LONG' else current < entry < stop)
    rules = SimpleNamespace(tick=tick, taker_fee_rate=rate, fee_observed_at=fee_observed,
        fee_symbol=symbol, fee_source=risk['fee_source'])
    try:
        proposed = namespace['normalized_reward_risk_tp'](entry, stop, side, rules,
            symbol=symbol, check_fresh=False)
    except namespace['Review']:
        raise NativeTPConfirmationUnavailable('NEUROBRO_TP_RULES_UNAVAILABLE') from None
    if current == proposed:
        return None
    def text(value):
        return format(value, 'f')
    return ('Konfirmasi TP final oleh Neurobro diperlukan untuk setup ' + symbol + ' ' + side
        + '. Jawabanmu menetapkan ENTRY ' + text(entry) + ', SL ' + text(stop) + ', TP ' + text(current)
        + '. Dengan fee akun ' + text(rate) + ' untuk entry/exit dan estimasi slippage exit '
        + text(namespace['EXIT_SLIPPAGE_RATE']) + ', rumus Risk Manager produksi menghitung TP eksekusi '
        + text(proposed) + ' agar RR bersih 1:2 sesudah fee dan slippage. '
        + 'TP belum diubah atau diotorisasi. Kamu tetap penentu setup. Jika setup masih valid, '
        + 'konfirmasikan TP final ' + text(proposed) + ' dalam jawaban lengkap LONG/SHORT; '
        + 'atau revisi ENTRY/SL dan tetapkan TP yang memenuhi perhitungan tersebut. '
        + 'Untuk jawaban lanjutan ini, isi field take_profit dengan TP final yang kamu setujui, '
        + 'bukan proposal awal; tidak ada TP eksekusi lain yang boleh menggantinya. '
        + 'Jika setup tidak valid, berikan HOLD. Quantity tetap dihitung Risk Manager dari anggaran risiko Office.')


class DelegatedDOMBridge:
    """Use native DOM completion/provenance code for the actual final answer."""
    def __init__(self, bridge, client, *, native=None, converter=None,
                 monotonic=time.monotonic, wall_clock=time.time,
                 fresh_charts=None, followup_receipt=None, daily_room=None):
        self.daily_room = daily_room
        self.native = native or importlib.import_module(type(bridge).__module__)
        self.bridge, self.client = bridge, client
        self.clock, self.wall_clock = monotonic, wall_clock
        if converter is None:
            from .native_market_context import convert_native_market_context
            converter = convert_native_market_context
        self.converter = converter
        # The authenticated native actor supplies these two application-scoped
        # operations. This DOM hook cannot fetch data, launch a browser or pick
        # an archive path. The existing operation deadline remains authoritative.
        if (fresh_charts is not None and not callable(fresh_charts)
                or followup_receipt is not None and not callable(followup_receipt)):
            raise ValueError('NATIVE_FOLLOWUP_CONFIGURATION_UNVERIFIED')
        self.fresh_charts, self.followup_receipt = fresh_charts, followup_receipt
        for name in ('ask', '_answer', '_turns', '_visible', 'status'):
            if not callable(getattr(bridge, name, None)):
                raise ValueError('NATIVE_BROWSER_HOOK_UNVERIFIED')
        for name in ('image_artifacts', 'exact_json', 'OutcomeUnknown'):
            if not callable(getattr(self.native, name, None)):
                raise ValueError('NATIVE_BROWSER_HOOK_UNVERIFIED')

    def __getattr__(self, name):
        return getattr(self.bridge, name)

    def ask(self, payload, dispatch_check, *, chart_images=(), chart_evidence=(),
            deadline=None, receipt_callback=None):
        if self.daily_room is not None:
            with self.daily_room.operation(payload):
                return self._ask(payload, dispatch_check, chart_images=chart_images,
                    chart_evidence=chart_evidence, deadline=deadline,
                    receipt_callback=receipt_callback)
        return self._ask(payload, dispatch_check, chart_images=chart_images,
            chart_evidence=chart_evidence, deadline=deadline,
            receipt_callback=receipt_callback)

    def _ask(self, payload, dispatch_check, *, chart_images=(), chart_evidence=(),
            deadline=None, receipt_callback=None):
        # Explicit native probe/evidence callbacks retain their existing path.
        # Normal BrowserActor._research has no callback and owns this transaction.
        if receipt_callback is not None:
            delivery = None
            if data_file_mode(self.native, payload):
                def callback_gate():
                    # Native OFF probe dispatch_check is a one-use consume,
                    # not a repeatable predicate. DOM.ask owns its sole call
                    # immediately before Send, as with the native PNG path.
                    if (self.bridge.status() != 'CONNECTED'
                            or deadline is not None and (type(deadline) not in (int, float)
                                or not math.isfinite(deadline) or self.clock() >= deadline)):
                        raise self.native.OutcomeUnknown()
                delivery = NativeDataFileDelivery(self.bridge, self.native, callback_gate,
                    wall_clock=self.wall_clock).install()
            try:
                return self.bridge.ask(payload, dispatch_check, chart_images=chart_images,
                    chart_evidence=chart_evidence, deadline=deadline,
                    receipt_callback=(delivery.receipt_callback(receipt_callback)
                        if delivery is not None else receipt_callback))
            finally:
                if delivery is not None:
                    delivery.close()
        if not callable(dispatch_check):
            raise ValueError('NATIVE_DISPATCH_CALLBACK_UNVERIFIED')
        started = self.clock()
        timeout = self.bridge.timeout
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
                or type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline <= started):
            raise ValueError('NATIVE_OPERATION_DEADLINE_UNVERIFIED')
        # A conservative subset of the native deadline; never extend its budget.
        until = min(deadline, started + timeout)
        original_answer = self.bridge._answer
        state = {'phase': 'INITIAL', 'previous': None, 'stable': 0, 'prefix': None,
                 'question': None, 'url': None, 'reviewed': False,
                 'fresh': None, 'fresh_names': [], 'bound_charts': None,
                 'followup_intents': [], 'initial_url': None, 'turn_names': []}
        payload_digest = _digest(payload)
        file_mode = data_file_mode(self.native, payload)
        delivery = None

        def unknown():
            raise self.native.OutcomeUnknown()

        def gate():
            if (self.clock() >= until or self.bridge.status() != 'CONNECTED'
                    or dispatch_check() is not True
                    or state['initial_url'] is not None and self.bridge.page.url != state['initial_url']):
                unknown()
            # Reuse the native PNG, timeframe, symbol, hash and age admission.
            self.native.image_artifacts(chart_images, chart_evidence)
            if state['fresh'] is not None:
                fresh = state['fresh']
                self.native.image_artifacts(fresh['chart_images'], fresh['chart_evidence'])
                # Never reinterpret the initial request, risk context or its
                # hash as a new request. Both contexts must remain fresh.
                self.converter(payload['context'], now=self.wall_clock())
                self.converter(fresh['context'], now=self.wall_clock())
            if _digest(payload) != payload_digest:
                unknown()

        def turns_with_names(names):
            previous_names = self.bridge.uploaded_names
            try:
                self.bridge.uploaded_names = list(names)
                return self.bridge._turns()
            finally:
                self.bridge.uploaded_names = previous_names

        def transcript(*, allow_pending=False):
            # Native attachment verification accepts names for one upload at a
            # time. Reconstruct each owned user turn with its own exact names;
            # never let the latest upload invalidate or disguise earlier turns.
            retries = 0
            while True:
                rows = turns_with_names([])
                prefix = state['prefix']
                if prefix is not None:
                    # A transient attachment projection cannot hide a changed
                    # message, role, completion flag or native message ID.
                    if len(rows) < len(prefix) or any(
                            {key: value for key, value in row.items() if key != 'attachments'}
                            != {key: value for key, value in expected.items() if key != 'attachments'}
                            for row, expected in zip(rows, prefix)):
                        unknown()
                pending_offset = len(prefix) if allow_pending and prefix is not None else None
                unfinished = pending_offset is not None and (len(rows) < pending_offset + 2
                    or rows[pending_offset+1].get('complete') is not True)
                missing = None
                for index, names in enumerate(state['turn_names']):
                    offset = index * 2
                    # The just-sent user turn/images and streaming answer can
                    # appear incrementally. The completed prefix stays strict.
                    if unfinished and offset == pending_offset:
                        continue
                    projected = turns_with_names(names)
                    if len(projected) != len(rows) and not (unfinished
                            and pending_offset <= len(projected) <= pending_offset + 2
                            and pending_offset <= len(rows) <= pending_offset + 2):
                        unknown()
                    if offset >= len(rows):
                        continue
                    for other in range(1, len(rows), 2):
                        if unfinished and other == pending_offset + 1:
                            continue
                        if projected[other] != rows[other]:
                            unknown()
                    row = projected[offset]
                    if prefix is not None and offset < len(prefix) and (
                            {key: value for key, value in row.items() if key != 'attachments'}
                            != {key: value for key, value in prefix[offset].items() if key != 'attachments'}):
                        unknown()
                    attachments = row.get('attachments', [])
                    if type(attachments) is not list:
                        unknown()
                    if sorted(attachments) != sorted(names):
                        # React can briefly remove an existing file tile while
                        # the next answer appears. Missing witnesses are pending,
                        # never accepted or replaced with cached attachments.
                        if not names or attachments not in ([], ['UNCONFIRMED']):
                            unknown()
                        missing = {'scope': 'PENDING_ATTACHMENT_READ_NOT_DELIVERY_PROOF',
                            'offset': offset, 'expected_names': list(names),
                            'observed_names': list(attachments), 'resamples': retries,
                            'prefix_message_fields_verified': prefix is not None}
                        self.bridge.native_attachment_projection = missing
                        if delivery is not None and delivery.observations:
                            delivery.observations[-1]['turn_projection'] = missing
                        break
                    rows[offset] = row
                if missing is None:
                    if retries:
                        gate()
                        self.bridge.native_attachment_projection.update(recovered=True, resamples=retries)
                    return rows
                gate()  # Current dispatch, URL and original deadline still own this read.
                retries += 1
                self.bridge.page.wait_for_timeout(25)

        def prepare_fresh_followup(question, market, prefix):
            # Question-only analysis is no longer admitted. A pre-200 release
            # with no actor callbacks fails closed before filling or sending a
            # second message; ordinary screening still needs no symbol chart.
            if (not callable(self.fresh_charts) or not callable(self.followup_receipt)
                    or not callable(getattr(self.native, 'user_prompt', None))
                    or not callable(getattr(self.bridge, '_upload', None))):
                unknown()
            gate()
            refresh_started = self.wall_clock()
            fresh = self.fresh_charts(payload=copy.deepcopy(payload), deadline=until,
                dispatch_check=dispatch_check)
            gate()
            if (type(fresh) is not dict
                    or set(fresh) != {'context', 'chart_images', 'chart_evidence'}
                    or type(fresh['context']) is not dict
                    or type(fresh['chart_images']) not in (list, tuple)
                    or type(fresh['chart_evidence']) not in (list, tuple)
                    or len(fresh['chart_images']) != 2 or len(fresh['chart_evidence']) != 2
                    or any(type(path) is not str or not path for path in fresh['chart_images'])):
                unknown()
            fresh = copy.deepcopy(fresh)
            current = self.wall_clock()
            projected = self.converter(fresh['context'], now=current)
            from .llm import validated_market_context
            validated_market_context(projected, now=current)
            if (projected['symbol'] != market['symbol']
                    or projected['observed_at'] < refresh_started
                    or projected['observed_at'] < market['observed_at']
                    or projected['server_time_ms'] < market['server_time_ms']
                    or not 0 <= current-projected['observed_at'] <= 180):
                unknown()
            fields = {'source', 'symbol', 'timeframe', 'captured_at', 'sha256', 'bytes'}
            from .native_chart_capture import SOURCE
            frames = set()
            for row in fresh['chart_evidence']:
                if (type(row) is not dict or set(row) != fields
                        or row['source'] != SOURCE or row['symbol'] != market['symbol']
                        or row['timeframe'] not in ('15m', '1h') or row['timeframe'] in frames):
                    unknown()
                captured = datetime.fromisoformat(row['captured_at'].replace('Z', '+00:00'))
                if (captured.tzinfo is None or captured.utcoffset() != timezone.utc.utcoffset(captured)
                        or not projected['observed_at'] <= captured.timestamp() <= current
                        or not 0 <= current-captured.timestamp() <= 180):
                    unknown()
                frames.add(row['timeframe'])
            artifacts = self.native.image_artifacts(fresh['chart_images'], fresh['chart_evidence'])
            if type(artifacts) is not list or len(artifacts) != 2:
                unknown()
            followup_payload = copy.deepcopy(payload)
            followup_payload['context'], followup_payload['prompt'] = fresh['context'], question
            rendered = self.native.user_prompt(followup_payload)
            if (type(rendered) is not str or not rendered.startswith(question)
                    or len(rendered.encode()) > 384*1024
                    or (not file_mode and json.dumps(projected, sort_keys=True, separators=(',', ':'),
                        ensure_ascii=False, allow_nan=False) not in rendered)):
                unknown()
            composers = self.bridge._visible(self.bridge._dom_contract.composer)
            if len(composers) != 1 or composers[0].input_value():
                unknown()
            gate()  # A PNG upload is an external browser action as well.
            bound = self.bridge._upload(composers[0], followup_payload, artifacts)
            names = list(self.bridge.uploaded_names)
            if (type(bound) is not list or len(bound) != 2 or len(names) != (4 if file_mode else 2)
                    or any(type(name) is not str or not name or len(name) > 240 for name in names)
                    or len(set(names)) != (4 if file_mode else 2)
                    or transcript() != prefix):
                unknown()
            identifiers = set()
            for row, witness in zip(bound, fresh['chart_evidence']):
                if (type(row) is not dict or set(row) != fields | {'attachment_id'}
                        or {key: row[key] for key in fields} != witness
                        or type(row['attachment_id']) is not str
                        or re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', row['attachment_id']) is None
                        or row['attachment_id'] in identifiers):
                    unknown()
                identifiers.add(row['attachment_id'])
            intent = {'schema_version': 1, 'kind': 'NATIVE_200_CHART_FOLLOWUP',
                'operation': payload['operation'], 'request_sha256': payload['request_sha256'],
                'marker': payload['marker'], 'original_payload_sha256': payload_digest,
                'initial_context_sha256': _digest(payload['context']),
                'followup_context_sha256': _digest(fresh['context']),
                'projected_context_sha256': _digest(projected),
                'initial_prompt_sha256': hashlib.sha256(state['prompt'].encode()).hexdigest(),
                'followup_prompt_sha256': hashlib.sha256(rendered.encode()).hexdigest(),
                'initial_reply_sha256': hashlib.sha256(state['previous'].encode()).hexdigest(),
                'initial_turns_sha256': _digest(prefix),
                'conversation_url_sha256': hashlib.sha256(state['initial_url'].encode()).hexdigest(),
                'initial_chart_evidence': copy.deepcopy(list(chart_evidence)),
                'initial_attachment_names': list(state['attachments']),
                'followup_chart_evidence': copy.deepcopy(bound), 'followup_attachment_names': names,
                'observed_at': projected['observed_at'], 'closed_candles_per_timeframe': 200,
                'closed_candles_total': 400}
            intent.update(followup_sequence=len(state['followup_intents']) + 1,
                previous_followup_intent_sha256=(
                    _digest(state['followup_intents'][-1]) if state['followup_intents'] else None))
            state.update(fresh=fresh, fresh_names=names, bound_charts=copy.deepcopy(bound))
            gate()
            # The actor must create this intent exclusively and durably before
            # any followup click. An existing/unknown intent cannot be replayed.
            if self.followup_receipt(stage='INTENT', receipt=copy.deepcopy(intent),
                    deadline=until) is not True:
                unknown()
            state['followup_intents'].append(intent)
            gate()
            return rendered

        def review(answer):
            gate()
            observed_turns = transcript()
            if not observed_turns or observed_turns[-1].get('text') != answer:
                unknown()
            required = payload.get('output_schema', {}).get('required')
            if type(required) is not list or any(type(key) is not str for key in required):
                unknown()
            phase = 'SCREENING' if set(required) == {'symbols'} else 'ANALYSIS'
            market = None if phase == 'SCREENING' else self.converter(
                state['fresh']['context'] if state['fresh'] is not None else payload['context'],
                now=self.wall_clock())
            operation = payload.get('operation')
            request_sha = payload.get('request_sha256')
            if type(operation) is not str or type(request_sha) is not str:
                unknown()
            reply_sha = hashlib.sha256(answer.encode()).hexdigest()
            identity = hashlib.sha256(json.dumps({'operation': operation,
                'request_sha256': request_sha, 'reply_sha256': reply_sha,
                'phase': phase, 'transcript_sha256': _digest(observed_turns)},
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            request = {'schema_version': 1, 'request_id': identity,
                'conversation_id': (self.daily_room.conversation_id if self.daily_room is not None
                    else operation), 'phase': phase, 'reply': answer,
                'reply_sha256': reply_sha, 'market_context': market,
                'gate': {'robot_on': True, 'native_dispatch_verified': True},
                'deadline_epoch': self.wall_clock() + min(90.0, until - self.clock())}
            # Independently enforce the dialogue-only result protocol, including
            # no foreign symbols, prices or trading authority from the model.
            from .dialogue_service import validate_request, validate_result
            validate_request(request, now=self.wall_clock())
            response = validate_result(self.client.request(request), request)
            gate()
            if transcript() != observed_turns:
                unknown()
            state['reviewed'] = True
            # The model cannot replace the native setup. A directional answer
            # must also name the exact TP the existing Risk Manager will use;
            # a different execution TP is discussed with Neurobro in this chat.
            try:
                output = self.native.exact_json(answer, payload['marker'])
            except (ValueError, TypeError, UnicodeError):
                output = None  # An incomplete parse never becomes an invented setup.
            complete = type(output) is dict and set(required).issubset(output)
            tp_question = None
            if complete and phase == 'ANALYSIS':
                try:
                    # A fresh chart callback contains public candles only. The
                    # immutable worker request owns the account fee/rule facts;
                    # the worker rechecks the final TP after its own fee/rule
                    # refresh before stamping, without expiring this discussion.
                    tp_question = _neurobro_tp_confirmation(output, payload['context'],
                        now=self.wall_clock())
                except NativeTPConfirmationUnavailable:
                    unknown()
            required_question = tp_question
            if phase == 'ANALYSIS' and not complete and not response['needs_followup']:
                # A model HOLD/COMPLETE decision cannot replace the native
                # marker/schema binding. Ask the WEB author to restate its own
                # answer; never turn an unbound fragment into a local setup.
                required_question = (
                    'Pertahankan keputusan dan level yang baru saja kamu berikan; '
                    'jangan membuat rekomendasi atau level baru. Tuliskan kembali jawaban '
                    'yang sama dalam JSON lengkap sesuai format permintaan awal. '
                    'Jika keputusanmu HOLD, tetap HOLD dengan level order null. '
                    'Sertakan marker persis dari permintaan awal: ' + payload['marker'])
            if required_question is None and (not response['needs_followup'] or complete):
                state['phase'] = 'ACCEPT'
                return
            if required_question is not None:
                # Required format/price clarification keeps the actual Mini
                # result and the original Neurobro answer untouched.
                question = required_question
            else:
                question = response['decision']['question']
                if response['decision']['action'] != 'CLARIFY' or not question:
                    unknown()
            prefix = copy.deepcopy(observed_turns)
            if (len(prefix) != 2 * len(state['turn_names'])
                    or prefix[-1].get('text') != answer
                    or prefix[0].get('role') == prefix[1].get('role')
                    or prefix[-1].get('complete') is not True):
                unknown()
            # Initial prompt and its schema/marker stay byte-for-byte unchanged.
            # Each new turn adds a question and the same format reminder.
            question += '\nGunakan kembali format JSON dan marker pada permintaan awal.'
            if phase != 'SCREENING':
                question = prepare_fresh_followup(question, market, prefix)
            composers = self.bridge._visible(self.bridge._dom_contract.composer)
            if len(composers) != 1 or composers[0].input_value():
                unknown()
            composer = composers[0]
            gate()
            composer.fill(question, timeout=self.bridge._timeout(5000))
            if (not _same_text(composer.input_value(), question)
                    or transcript() != prefix):
                unknown()
            buttons = self.bridge._visible(self.bridge._dom_contract.send)
            if len(buttons) != 1 or not buttons[0].is_enabled():
                unknown()
            gate()  # Last possible current ROBOT/dispatch/age check before click.
            state.update(phase='FOLLOWUP_SENT', prefix=prefix, question=question,
                         url=self.bridge.page.url, previous=None, stable=0)
            state['turn_names'].append(list(state['fresh_names']))
            buttons[0].click(timeout=self.bridge._timeout(5000))

        def final_answer():
            if self.bridge.page.url != state['url']:
                unknown()
            # A send may have only its user turn visible while the reply starts.
            turns = transcript(allow_pending=True)
            offset = len(state['prefix'])
            if len(turns) > offset + 2 or turns[:offset] != state['prefix']:
                unknown()
            if len(turns) < offset + 2 or turns[offset+1].get('complete') is not True:
                return None
            if (turns[offset].get('role') != state['prefix'][0].get('role')
                    or not _same_text(turns[offset].get('text'), state['question'])
                    or turns[offset+1].get('role') != state['prefix'][1].get('role')):
                unknown()
            answer = turns[offset+1].get('text')
            if (type(answer) is not str or not answer
                    or len(answer.encode()) > self.native.MAX_ANSWER):
                unknown()
            return answer

        def delegated_answer(prompt, attachment_names=()):
            if self.clock() >= until:
                unknown()
            if delivery is not None:
                attachment_names = delivery.attachment_names(attachment_names)
            if not state['turn_names']:
                state.update(prompt=prompt, attachments=attachment_names)
                state['turn_names'] = [list(attachment_names)]
            answer = (final_answer() if state['prefix'] is not None
                      else original_answer(prompt, attachment_names))
            if state['phase'] == 'ACCEPT':
                return answer
            state['stable'] = state['stable'] + 1 if answer and answer == state['previous'] else 0
            state['previous'] = answer
            if answer is None or state['stable'] < 3:
                return None
            if state['initial_url'] is None:
                state['initial_url'] = self.bridge.page.url
            review(answer)
            return answer if state['phase'] == 'ACCEPT' else None

        if file_mode:
            delivery = NativeDataFileDelivery(self.bridge, self.native, gate,
                wall_clock=self.wall_clock).install()
        self.bridge._answer = delegated_answer
        try:
            # The original native implementation still sends the first prompt,
            # waits for stable completion and creates the FINAL receipt/hash/ID.
            result = self.bridge.ask(payload, dispatch_check, chart_images=chart_images,
                chart_evidence=chart_evidence, deadline=deadline, receipt_callback=None)
            if state['followup_intents']:
                try:
                    gate()
                    if (type(result) is not dict or type(result.get('assistant_text')) is not str
                            or not result['assistant_text']
                            or type(result.get('response_id')) is not str
                            or re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', result['response_id']) is None
                            or result['assistant_text'] != final_answer()):
                        unknown()
                    for intent in state['followup_intents']:
                        complete = {**intent, 'intent_sha256': _digest(intent),
                            'response_id': result['response_id'],
                            'final_reply_sha256': hashlib.sha256(result['assistant_text'].encode()).hexdigest()}
                        if self.followup_receipt(stage='COMPLETE', receipt=copy.deepcopy(complete),
                                deadline=until) is not True:
                            unknown()
                    gate()
                    # The unchanged native receipt code owns the final response
                    # ID and answer. Its existing worker journal receives the
                    # PNG receipts actually attached to that final discussion.
                    result = {**result, 'chart_evidence': copy.deepcopy(state['bound_charts'])}
                except Exception:
                    unknown()
            return result
        finally:
            self.bridge._answer = original_answer
            if delivery is not None:
                delivery.close()


def install_native_bridge(bridge, *, fresh_charts=None, followup_receipt=None, archive_root=None):
    """An unconfigured additive hook preserves the existing production bridge."""
    if (fresh_charts is not None and not callable(fresh_charts)
            or followup_receipt is not None and not callable(followup_receipt)):
        raise ValueError('NATIVE_FOLLOWUP_CONFIGURATION_UNVERIFIED')
    path = os.environ.get('HARUN_AGENT_DIALOGUE_SOCKET')
    token = os.environ.get('HARUN_AGENT_DIALOGUE_TOKEN_FILE')
    if not path and not token:
        if fresh_charts is not None or followup_receipt is not None:
            raise ValueError('NATIVE_DIALOGUE_CONFIGURATION_UNVERIFIED')
        return bridge
    if not path or not token:
        raise ValueError('NATIVE_DIALOGUE_CONFIGURATION_UNVERIFIED')
    if isinstance(bridge, DelegatedDOMBridge):
        if (fresh_charts is not None and bridge.fresh_charts is not fresh_charts
                or followup_receipt is not None and bridge.followup_receipt is not followup_receipt):
            raise ValueError('NATIVE_FOLLOWUP_CONFIGURATION_UNVERIFIED')
        return bridge
    from .dialogue_client import DialogueClient
    daily_room = None
    if archive_root is not None:
        from .native_daily_room import DailyRoom
        native = importlib.import_module(type(bridge).__module__)
        daily_room = DailyRoom(bridge, native, archive_root)
    return DelegatedDOMBridge(bridge, DialogueClient(path, token),
        fresh_charts=fresh_charts, followup_receipt=followup_receipt,
        daily_room=daily_room)
