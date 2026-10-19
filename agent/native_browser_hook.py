"""Add one delegated clarification inside the existing native browser transaction.

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
import time


def _same_text(left, right):
    return (type(left) is str and type(right) is str
            and left.replace('\r\n', '\n').strip() == right.replace('\r\n', '\n').strip())


class DelegatedDOMBridge:
    """Use native DOM completion/provenance code for the actual final answer."""
    def __init__(self, bridge, client, *, native=None, converter=None,
                 monotonic=time.monotonic, wall_clock=time.time):
        self.native = native or importlib.import_module(type(bridge).__module__)
        self.bridge, self.client = bridge, client
        self.clock, self.wall_clock = monotonic, wall_clock
        if converter is None:
            from .native_market_context import convert_native_market_context
            converter = convert_native_market_context
        self.converter = converter
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
            return self.bridge.ask(payload, dispatch_check, chart_images=chart_images,
                chart_evidence=chart_evidence, deadline=deadline, receipt_callback=receipt_callback)
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
                 'question': None, 'url': None, 'reviewed': False}

        def unknown():
            raise self.native.OutcomeUnknown()

        def gate():
            if (self.clock() >= until or self.bridge.status() != 'CONNECTED'
                    or dispatch_check() is not True):
                unknown()
            # Reuse the native PNG, timeframe, symbol, hash and age admission.
            self.native.image_artifacts(chart_images, chart_evidence)

        def review(answer):
            gate()
            required = payload.get('output_schema', {}).get('required')
            if type(required) is not list or any(type(key) is not str for key in required):
                unknown()
            phase = 'SCREENING' if set(required) == {'symbols'} else 'ANALYSIS'
            market = None if phase == 'SCREENING' else self.converter(
                payload['context'], now=self.wall_clock())
            operation = payload.get('operation')
            request_sha = payload.get('request_sha256')
            if type(operation) is not str or type(request_sha) is not str:
                unknown()
            reply_sha = hashlib.sha256(answer.encode()).hexdigest()
            identity = hashlib.sha256(json.dumps({'operation': operation,
                'request_sha256': request_sha, 'reply_sha256': reply_sha,
                'phase': phase}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            request = {'schema_version': 1, 'request_id': identity,
                'conversation_id': operation, 'phase': phase, 'reply': answer,
                'reply_sha256': reply_sha, 'market_context': market,
                'gate': {'robot_on': True, 'native_dispatch_verified': True},
                'deadline_epoch': self.wall_clock() + min(90.0, until - self.clock())}
            # Independently enforce the dialogue-only result protocol, including
            # no foreign symbols, prices or trading authority from the model.
            from .dialogue_service import validate_request, validate_result
            validate_request(request)
            response = validate_result(self.client.request(request), request)
            gate()
            state['reviewed'] = True
            # Complete native structured answers proceed immediately. The model
            # cannot force a discussion or override an existing complete setup.
            try:
                output = self.native.exact_json(answer, payload['marker'])
            except (ValueError, TypeError, UnicodeError):
                output = None  # An incomplete parse never becomes an invented setup.
            complete = type(output) is dict and set(required).issubset(output)
            if not response['needs_followup'] or complete:
                state['phase'] = 'ACCEPT_INITIAL'
                return
            question = response['decision']['question']
            if response['decision']['action'] != 'CLARIFY' or not question:
                unknown()
            prefix = copy.deepcopy(self.bridge._turns())
            if (len(prefix) != 2 or original_answer(state['prompt'], state['attachments']) != answer
                    or prefix[0].get('role') == prefix[1].get('role')
                    or prefix[1].get('complete') is not True):
                unknown()
            # Initial prompt and its schema/marker stay byte-for-byte unchanged.
            # The only new text is one question and a format reminder in the same chat.
            question += '\nGunakan kembali format JSON dan marker pada permintaan awal.'
            composers = self.bridge._visible(self.bridge._dom_contract.composer)
            if len(composers) != 1 or composers[0].input_value():
                unknown()
            composer = composers[0]
            composer.fill(question, timeout=self.bridge._timeout(5000))
            if (not _same_text(composer.input_value(), question)
                    or self.bridge._turns() != prefix):
                unknown()
            buttons = self.bridge._visible(self.bridge._dom_contract.send)
            if len(buttons) != 1 or not buttons[0].is_enabled():
                unknown()
            gate()  # Last possible current ROBOT/dispatch/age check before click.
            state.update(phase='FOLLOWUP_SENT', prefix=prefix, question=question,
                         url=self.bridge.page.url)
            buttons[0].click(timeout=self.bridge._timeout(5000))

        def final_answer():
            if self.bridge.page.url != state['url']:
                unknown()
            turns = self.bridge._turns()
            if len(turns) > 4 or turns[:2] != state['prefix']:
                unknown()
            if len(turns) < 4:
                return None
            # Native _turns uses uploaded_names to attest user attachments. Read
            # the new user turn once with zero expected attachments; the first
            # two immutable turns were verified above with their original names.
            names = self.bridge.uploaded_names
            try:
                self.bridge.uploaded_names = []
                without_uploads = self.bridge._turns()
            finally:
                self.bridge.uploaded_names = names
            if (len(without_uploads) != 4 or turns[2].get('role') != state['prefix'][0].get('role')
                    or without_uploads[2].get('role') != state['prefix'][0].get('role')
                    or not _same_text(turns[2].get('text'), state['question'])
                    or not _same_text(without_uploads[2].get('text'), state['question'])
                    or without_uploads[2].get('attachments') != []
                    or turns[3] != without_uploads[3]
                    or turns[3].get('role') != state['prefix'][1].get('role')):
                unknown()
            if turns[3].get('complete') is not True:
                return None
            answer = turns[3].get('text')
            if (type(answer) is not str or not answer
                    or len(answer.encode()) > self.native.MAX_ANSWER):
                unknown()
            return answer

        def delegated_answer(prompt, attachment_names=()):
            if self.clock() >= until:
                unknown()
            if state['phase'] == 'FOLLOWUP_SENT':
                return final_answer()
            answer = original_answer(prompt, attachment_names)
            if state['phase'] == 'ACCEPT_INITIAL':
                return answer
            state['stable'] = state['stable'] + 1 if answer and answer == state['previous'] else 0
            state['previous'] = answer
            if answer is None or state['stable'] < 3:
                return None
            state.update(prompt=prompt, attachments=attachment_names)
            review(answer)
            return answer if state['phase'] == 'ACCEPT_INITIAL' else None

        self.bridge._answer = delegated_answer
        try:
            # The original native implementation still sends the first prompt,
            # waits for stable completion and creates the FINAL receipt/hash/ID.
            return self.bridge.ask(payload, dispatch_check, chart_images=chart_images,
                chart_evidence=chart_evidence, deadline=deadline, receipt_callback=None)
        finally:
            self.bridge._answer = original_answer


def install_native_bridge(bridge):
    """An unconfigured additive hook preserves the existing production bridge."""
    path = os.environ.get('HARUN_AGENT_DIALOGUE_SOCKET')
    token = os.environ.get('HARUN_AGENT_DIALOGUE_TOKEN_FILE')
    if not path and not token:
        return bridge
    if not path or not token:
        raise ValueError('NATIVE_DIALOGUE_CONFIGURATION_UNVERIFIED')
    if isinstance(bridge, DelegatedDOMBridge):
        return bridge
    from .dialogue_client import DialogueClient
    return DelegatedDOMBridge(bridge, DialogueClient(path, token))
