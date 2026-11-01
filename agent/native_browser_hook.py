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
from .neurobro_reply import ReplyIncomplete, parse_reply_for_payload


# A technical budget for one native browser RPC, not a discussion-round or
# trading limit. Each model call and each material Send keeps its own checks.
OWNER_DIALOGUE_TIMEOUT_SECONDS = 900


def _same_text(left, right):
    return (type(left) is str and type(right) is str
            and left.replace('\r\n', '\n').strip() == right.replace('\r\n', '\n').strip())


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode('ascii')


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _source_facts(payload):
    from .dialogue_service import FACT_KEYS, validate_source_facts
    context = payload.get('context') or {}
    result = []
    for name, producer in (('risk_manager_facts', 'RISK_MANAGER'),
            ('pending_order_context', 'OWNED_PENDING_ORDER')):
        values = context.get(name)
        if isinstance(values, dict):
            copied = {key: copy.deepcopy(value) for key, value in values.items() if key in FACT_KEYS}
            result.append({'producer': producer, 'operation': payload['operation'],
                'values': copied, 'sha256': _digest(copied)})
    return validate_source_facts(result)


def _phase(payload, required):
    from .native_pending_protocol import dialogue_phase_for_payload
    return dialogue_phase_for_payload(payload, required)


class NativeTPConfirmationUnavailable(ValueError):
    pass


def _neurobro_tp_confirmation(output, context, *, now):
    """Compatibility shim: execution TP is now the owner's Risk Manager policy.

    Neurobro's original recommendation stays intact. Normalizing only execution
    TP must not create another chat round or ask Neurobro to revise ENTRY/SL.
    """
    return None


class DelegatedDOMBridge:
    """Use native DOM completion/provenance code for the actual final answer."""
    def __init__(self, bridge, client, *, native=None, converter=None,
                 monotonic=time.monotonic, wall_clock=time.time,
                 fresh_charts=None, followup_receipt=None, daily_room=None,
                 native_turn_batch=None, compose_enabled=False):
        self.daily_room = daily_room
        self.native_turn_batch = native_turn_batch
        self.native = native or importlib.import_module(type(bridge).__module__)
        self.bridge, self.client = bridge, client
        self.clock, self.wall_clock = monotonic, wall_clock
        if converter is None:
            from .native_market_context import convert_native_market_context
            converter = convert_native_market_context
        self.converter = converter
        self.prompt_cache = None
        if compose_enabled:
            if daily_room is None or not callable(getattr(native or self.native, "user_prompt", None)):
                raise ValueError("NATIVE_COMPOSE_CONFIGURATION_UNVERIFIED")
            from .native_prompt_composition import PromptCompositionCache
            self.prompt_cache = PromptCompositionCache(self.native, daily_room.store.root)
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
        if self.native_turn_batch is not None:
            self.native_turn_batch.begin(payload)
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
        original_exact_json = self.native.exact_json
        state = {'phase': 'INITIAL', 'previous': None, 'stable': 0, 'prefix': None,
                 'question': None, 'url': None, 'reviewed': False,
                 'fresh': None, 'fresh_names': [], 'bound_charts': None,
                 'followup_intents': [], 'initial_url': None, 'turn_names': [],
                 'accepted_reply_sha256': None, 'accepted_output': None,
                 'asked_questions': []}
        payload_digest = _digest(payload)
        file_mode = data_file_mode(self.native, payload)
        delivery = None

        def unknown():
            raise self.native.OutcomeUnknown()

        def gate(*, check_material=False):
            if (self.clock() >= until or self.bridge.status() != 'CONNECTED'
                    or dispatch_check() is not True
                    or state['initial_url'] is not None and self.bridge.page.url != state['initial_url']):
                unknown()
            if check_material:
                # Reuse the native PNG, timeframe, symbol, hash and age admission.
                if state['fresh'] is not None:
                    fresh = state['fresh']
                    self.native.image_artifacts(fresh['chart_images'], fresh['chart_evidence'])
                    # The immutable original request remains in the journal. It is
                    # not the current chart after a verified refresh has replaced
                    # the discussion material; do not expire that fresh material
                    # merely because the original chart is now old.
                    self.converter(fresh['context'], now=self.wall_clock())
                else:
                    self.native.image_artifacts(chart_images, chart_evidence)
                if self.prompt_cache is not None and payload.get('context') is not None:
                    from .native_chart_followup import material_dispatch_provenance
                    active = state['fresh']
                    context = active['context'] if active is not None else payload['context']
                    evidence = active['chart_evidence'] if active is not None else chart_evidence
                    # New composed workflow requires website screenshots. The old
                    # renderer remains admitted only on its unchanged legacy path.
                    state['material_send_check'] = material_dispatch_provenance(
                        context, evidence, now=self.wall_clock())
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
            expected_source = getattr(self.fresh_charts, 'expected_source', None)
            if type(expected_source) is not str:
                expected_source = SOURCE
            frames = set()
            for row in fresh['chart_evidence']:
                if (type(row) is not dict or set(row) != fields
                        or row['source'] != expected_source or row['symbol'] != market['symbol']
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
            gate(check_material=True)
            if self.prompt_cache is not None and state.get('material_send_check') is not None:
                intent['material_send_check'] = copy.deepcopy(state['material_send_check'])
            # The actor must create this intent exclusively and durably before
            # any followup click. An existing/unknown intent cannot be replayed.
            if self.followup_receipt(stage='INTENT', receipt=copy.deepcopy(intent),
                    deadline=until) is not True:
                unknown()
            state['followup_intents'].append(intent)
            gate()
            return rendered

        def send_followup(question, market, observed_turns, answer, phase):
            prefix = copy.deepcopy(observed_turns)
            if (len(prefix) != 2 * len(state['turn_names'])
                    or prefix[-1].get('text') != answer
                    or prefix[0].get('role') == prefix[1].get('role')
                    or prefix[-1].get('complete') is not True):
                unknown()
            # The operation remains bound by native transcript/receipts. Human
            # discussion needs no repeated JSON schema or marker code block.
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
            gate(check_material=True)  # Actual Send boundary owns current material freshness.
            state.update(phase='FOLLOWUP_SENT', prefix=prefix, question=question,
                         url=self.bridge.page.url, previous=None, stable=0)
            state['turn_names'].append(list(state['fresh_names']))
            buttons[0].click(timeout=self.bridge._timeout(5000))

        def review(answer):
            gate()
            observed_turns = transcript()
            if not observed_turns or observed_turns[-1].get('text') != answer:
                unknown()
            required = payload.get('output_schema', {}).get('required')
            if type(required) is not list or any(type(key) is not str for key in required):
                unknown()
            phase = _phase(payload, required)
            active_context = state['fresh']['context'] if state['fresh'] is not None else payload.get('context')
            if phase != 'SCREENING':
                observed = active_context.get('fetched_at') if isinstance(active_context, dict) else None
                if (type(observed) in (int, float) and math.isfinite(observed)
                        and self.wall_clock() - observed > 180):
                    # The reply is known, not an uncertain Send. Refresh before
                    # accepting a market decision from old charts; timestamps
                    # remain their real production values throughout.
                    reference = {'symbol': active_context.get('symbol'),
                        'observed_at': observed, 'server_time_ms': active_context.get('server_time')}
                    send_followup('Materi pasar sebelumnya sudah berusia lebih lama. '
                        'Berikut chart dan data terbaru untuk coin yang sama; mohon perbarui '
                        'keputusan berdasarkan kondisi terkini dan pertahankan konteks pembahasannya.',
                        reference, observed_turns, answer, phase)
                    return
            market = None if phase == 'SCREENING' else self.converter(active_context, now=self.wall_clock())
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
            facts = _source_facts(payload)
            if facts:
                request['source_facts'] = facts
            # Independently enforce the dialogue-only result protocol, including
            # no foreign symbols, prices or trading authority from the model.
            from .dialogue_service import validate_request, validate_result
            validate_request(request, now=self.wall_clock())
            response = validate_result(self.client.request(request), request)
            gate()
            if transcript() != observed_turns:
                unknown()
            state['reviewed'] = True
            # Interpret only this actual reply. Internal schema/marker data is
            # owned by this transaction, not another JSON demand in WEB chat.
            try:
                output = self.native.exact_json(answer, payload['marker'])
            except (ValueError, TypeError, UnicodeError):
                output = None  # An incomplete parse never becomes an invented setup.
            parse_error = None
            natural_parsed = False
            try:
                output = parse_reply_for_payload(answer, payload)
                natural_parsed = True
            except (ReplyIncomplete, KeyError) as error:
                parse_error = str(error)
                if parse_error == 'NEUROBRO_LEVEL_PRECISION_UNSUPPORTED':
                    # This transport cannot represent the authored level. Do
                    # not round it or ask Neurobro to move ENTRY/SL to fit.
                    raise ValueError(parse_error) from None
                if parse_error in {'NEUROBRO_INPUT_UNREADABLE', 'NEUROBRO_DECISION_CONFLICT',
                        'NEUROBRO_SETUP_CONDITIONAL', 'NEUROBRO_REVIEW_CONDITIONAL'}:
                    output = None  # Do not fall back to an old/conflicting card.
            complete = type(output) is dict and set(required).issubset(output)
            expected_symbol = (payload.get('context') or {}).get('symbol')
            if complete and phase != 'SCREENING' and output.get('symbol') != expected_symbol:
                output, complete, parse_error = None, False, 'NEUROBRO_FOREIGN_COIN'
            required_question = None
            action = response['decision']['action']
            if complete and natural_parsed and phase == 'ANALYSIS' and (
                    action == 'HOLD' and output.get('side') != 'HOLD'
                    or action == 'COMPLETE' and output.get('side') == 'HOLD'):
                complete, output, parse_error = False, None, 'NEUROBRO_DECISION_CONFLICT'
            if (complete and natural_parsed and phase == 'PENDING_REVIEW'
                    and action in {'TAHAN', 'BATALKAN'} and action != output.get('decision')):
                complete, output, parse_error = False, None, 'NEUROBRO_DECISION_CONFLICT'
            if (phase == 'SCREENING' and not complete and not response['needs_followup']
                    and self.prompt_cache is not None):
                limits = payload.get('output_schema', {}).get('properties', {}).get('symbols', {})
                target = limits.get('minItems')
                count = str(target) + ' ' if type(target) is int else ''
                required_question = ('Mohon ringkas ' + count + 'coin pilihanmu sesuai kapasitas '
                    'yang diminta dalam bentuk COIN: simbol USDT Perpetual. '
                    'Jangan sertakan coin pembanding sebagai pilihan.')
            if phase != 'SCREENING' and not complete and not response['needs_followup']:
                # A model HOLD/COMPLETE decision cannot replace the native
                # marker/schema binding. Ask the WEB author to restate its own
                # answer; never turn an unbound fragment into a local setup.
                required_question = (
                    ('Lampiran atau data belum dapat dipahami dengan benar. Mohon jelaskan '
                     'bagian yang belum terbaca atau tidak konsisten sebelum memberi keputusan pasar. '
                     if parse_error == 'NEUROBRO_INPUT_UNREADABLE' else
                     'Mohon pertegas keputusan akhir untuk coin yang sedang dibahas, tanpa '
                     'mengambil level coin lain atau skenario bersyarat. ')
                    + ('Ringkas COIN dan KEPUTUSAN: TAHAN/BATALKAN untuk order ini.'
                       if phase == 'PENDING_REVIEW' else
                       'Ringkas COIN, ARAH LONG/SHORT/HOLD, ENTRY, TP, dan SL. '
                       'Pertahankan level yang memang sudah menjadi keputusanmu.'))
            legacy_complete = complete and self.prompt_cache is None and not natural_parsed and parse_error not in {
                'NEUROBRO_INPUT_UNREADABLE', 'NEUROBRO_DECISION_CONFLICT', 'NEUROBRO_SETUP_CONDITIONAL'}
            if required_question is None and (not response['needs_followup'] or legacy_complete):
                state.update(phase='ACCEPT', accepted_reply_sha256=reply_sha,
                    accepted_output=copy.deepcopy(output) if complete else None)
                return
            if required_question is not None:
                # Required format/price clarification keeps the actual Mini
                # result and the original Neurobro answer untouched.
                question = required_question
            else:
                question = response['decision']['question']
                if response['decision']['action'] != 'CLARIFY' or not question:
                    unknown()
            if question in state['asked_questions']:
                if complete:
                    state.update(phase='ACCEPT', accepted_reply_sha256=reply_sha,
                        accepted_output=copy.deepcopy(output))
                    return
            state['asked_questions'].append(question)
            send_followup(question, market, observed_turns, answer, phase)

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
                gate()
                if answer is not None and hashlib.sha256(answer.encode()).hexdigest() != state['accepted_reply_sha256']:
                    unknown()
                return answer
            state['previous'] = answer
            # The native reader already requires the actual completion footer,
            # and review binds the live transcript before and after Mini. The
            # unchanged native ask loop owns its three stable final polls; do
            # not repeat those full daily-history reads before calling Mini.
            if answer is None:
                return None
            if state['initial_url'] is None:
                state['initial_url'] = self.bridge.page.url
            review(answer)
            return answer if state['phase'] == 'ACCEPT' else None

        def operation_exact_json(answer, marker):
            # The real native ask calls exact_json inside its return statement,
            # before this wrapper can inspect that result. Project only the
            # already accepted, actually observed reply for this operation;
            # every other answer/marker still uses the original native parser.
            if (state['phase'] == 'ACCEPT' and marker == payload['marker']
                    and state['accepted_output'] is not None and type(answer) is str
                    and hashlib.sha256(answer.encode()).hexdigest() == state['accepted_reply_sha256']):
                return copy.deepcopy(state['accepted_output'])
            return original_exact_json(answer, marker)

        if file_mode:
            delivery = NativeDataFileDelivery(self.bridge, self.native, gate,
                wall_clock=self.wall_clock).install()
        self.bridge._answer = delegated_answer
        self.native.exact_json = operation_exact_json
        try:
            if self.prompt_cache is not None and self.prompt_cache.read(payload) is None:
                # Draft once before the native Send. The durable render receipt
                # makes DailyRoom and native receipt checks see identical bytes.
                try:
                    gate(check_material=True)
                    required = payload.get('output_schema', {}).get('required', [])
                    phase = _phase(payload, required)
                    prompt = payload['prompt']
                    market = None if phase == 'SCREENING' else self.converter(
                        payload['context'], now=self.wall_clock())
                    identity = _digest({'kind': 'MINI_COMPOSE_V1', 'payload_sha256': payload_digest})
                    compose = {'schema_version': 1, 'request_id': identity,
                        'conversation_id': 'compose:' + hashlib.sha256(payload['operation'].encode()).hexdigest(),
                        'phase': 'COMPOSE_' + phase, 'reply': prompt,
                        'reply_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                        'market_context': market,
                        'gate': {'robot_on': True, 'native_dispatch_verified': True},
                        'deadline_epoch': self.wall_clock() + min(90.0, until - self.clock())}
                    facts = _source_facts(payload)
                    if facts: compose['source_facts'] = facts
                    from .dialogue_service import validate_request, validate_result
                    validate_request(compose, now=self.wall_clock())
                    result = validate_result(self.client.compose(compose), compose)
                    if result['decision']['action'] != 'PROMPT':
                        raise ValueError('NATIVE_COMPOSE_RESULT_UNVERIFIED')
                    gate(check_material=True)
                    self.prompt_cache.store(payload, result['decision']['question'], identity)
                    gate(check_material=True)
                except Exception:
                    raise self.native.NotSubmitted('UNAVAILABLE') from None
            # The original native implementation still sends the first prompt,
            # waits for stable completion and creates the FINAL receipt/hash/ID.
            try:
                gate(check_material=True)
            except Exception:
                status = self.bridge.status()
                raise self.native.NotSubmitted(status if status in
                    {'LOGIN_REQUIRED', 'CHALLENGE_REQUIRED'} else 'UNAVAILABLE') from None
            result = self.bridge.ask(payload, dispatch_check, chart_images=chart_images,
                chart_evidence=chart_evidence, deadline=deadline, receipt_callback=None)
            if state['accepted_output'] is not None:
                if (type(result) is not dict or type(result.get('assistant_text')) is not str
                        or hashlib.sha256(result['assistant_text'].encode()).hexdigest()
                            != state['accepted_reply_sha256']):
                    unknown()
                result = {**result, 'output': copy.deepcopy(state['accepted_output'])}
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
            if self.native_turn_batch is not None:
                self.native_turn_batch.evidence('COMPLETE')
            return result
        except BaseException as error:
            try:
                from .native_turn_batch import retain_return_failure
                root = self.daily_room.store.path.parent if self.daily_room is not None else None
                retain_return_failure(root, payload, state, self.clock(), until, error)
                if self.native_turn_batch is not None:
                    self.native_turn_batch.evidence('FAILURE')
            except BaseException:
                pass
            raise
        finally:
            self.bridge._answer = original_answer
            self.native.exact_json = original_exact_json
            if delivery is not None:
                delivery.close()


def install_native_bridge(bridge, *, fresh_charts=None, followup_receipt=None, archive_root=None,
                          dialogue_timeout_seconds=OWNER_DIALOGUE_TIMEOUT_SECONDS):
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
    native_turn_batch = None
    if archive_root is not None:
        if dialogue_timeout_seconds != OWNER_DIALOGUE_TIMEOUT_SECONDS:
            raise ValueError('NATIVE_DIALOGUE_TIMEOUT_UNVERIFIED')
        bridge.timeout = dialogue_timeout_seconds
        from .native_daily_room import DailyRoom
        from .native_turn_batch import install_native_turn_batch
        native = importlib.import_module(type(bridge).__module__)
        native_turn_batch = install_native_turn_batch(bridge, native, archive_root)
        daily_room = DailyRoom(bridge, native, archive_root)
    return DelegatedDOMBridge(bridge, DialogueClient(path, token),
        fresh_charts=fresh_charts, followup_receipt=followup_receipt,
        daily_room=daily_room, native_turn_batch=native_turn_batch,
        compose_enabled=archive_root is not None)
