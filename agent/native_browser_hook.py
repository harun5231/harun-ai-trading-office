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


class DelegatedDOMBridge:
    """Use native DOM completion/provenance code for the actual final answer."""
    def __init__(self, bridge, client, *, native=None, converter=None,
                 monotonic=time.monotonic, wall_clock=time.time,
                 fresh_charts=None, followup_receipt=None):
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
            rows = turns_with_names([])
            pending_offset = len(state['prefix']) if allow_pending and state['prefix'] is not None else None
            unfinished = pending_offset is not None and (len(rows) < pending_offset + 2
                or rows[pending_offset+1].get('complete') is not True)
            for index, names in enumerate(state['turn_names']):
                offset = index * 2
                # The just-sent user turn/images and streaming answer can appear
                # incrementally. Wait for completion before attesting this pair;
                # the previously completed prefix remains strict at every poll.
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
                if sorted(row.get('attachments', [])) != sorted(names):
                    unknown()
                rows[offset] = row
            return rows

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
                'conversation_id': operation, 'phase': phase, 'reply': answer,
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
            # Complete native structured answers proceed immediately. The model
            # cannot force a discussion or override an existing complete setup.
            try:
                output = self.native.exact_json(answer, payload['marker'])
            except (ValueError, TypeError, UnicodeError):
                output = None  # An incomplete parse never becomes an invented setup.
            complete = type(output) is dict and set(required).issubset(output)
            if not response['needs_followup'] or complete:
                state['phase'] = 'ACCEPT'
                return
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


def install_native_bridge(bridge, *, fresh_charts=None, followup_receipt=None):
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
    return DelegatedDOMBridge(bridge, DialogueClient(path, token),
        fresh_charts=fresh_charts, followup_receipt=followup_receipt)
