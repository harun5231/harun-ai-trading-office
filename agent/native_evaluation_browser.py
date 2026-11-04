"""Historical SAME-ROOM custody within the existing DailyRoom transaction.

No browser, model or coordinator is created. The original historical four-file
turn must still be genuinely available in this room. Missing/other-room material
is blocked; current market images never substitute for historical evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json

from .native_evaluation_protocol import envelope, PHASE
from worker.trade_evaluation_evidence import digest, need


def _text_sha(value):
    return hashlib.sha256(value.replace('\r\n', '\n').strip().encode()).hexdigest()


class HistoricalEvaluation:
    def __init__(self, owner, payload, deadline):
        self.owner, self.payload, self.deadline = owner, payload, deadline
        self.envelope = envelope(payload)
        self.request = self.envelope['request']
        room = owner.daily_room
        need(room is not None and room._connection is not None
            and callable(owner.followup_receipt), 'EVALUATION_DAILY_ROOM_REQUIRED')
        selected = room._connection.execute('SELECT room_id,state FROM daily_rooms WHERE day=?', (room.day,)).fetchone()
        need(selected is not None and selected[0] == self.request['room_id'] and selected[1] == 'BOUND',
            'EVALUATION_BOUND_ROOM_NOT_AVAILABLE')
        self.intents = []
        self.custody = None

    def verify_material_access(self):
        """Check actual native decoded PNGs/CSV tiles and original reply.

        The saved receipt proves byte custody; this UI check independently
        proves Neurobro still has the SAME historical turn in the active room.
        It never treats a digest or file path as readable historical content.
        """
        from worker.trade_evaluation_archive import read_bound_material_turn
        from .native_file_upload import FILE_WITNESS, NativeDataFileDelivery
        room = self.owner.daily_room
        bridge, native = self.owner.bridge, self.owner.native
        need(room.room_id == self.request['room_id'] and room._prepared,
            'EVALUATION_BOUND_ROOM_NOT_AVAILABLE')
        previous_names = bridge.uploaded_names
        witnesses = []
        try:
            for material, reply in zip(self.request['historical_materials'], self.request['source_neurobro_replies']):
                receipt = read_bound_material_turn(room.store.root, material)
                leaf = receipt['intent']
                names = leaf['followup_attachment_names']
                pngs = [name for name in names if name.endswith('.png')]
                csvs = [name for name in names if name.endswith('.csv')]
                need(len(pngs) == len(csvs) == 2, 'EVALUATION_HISTORICAL_FOUR_FILES_REQUIRED')
                bridge.uploaded_names = list(pngs)
                rows = room.full_turns()
                turns = room.original_visible(native.MESSAGE)
                need(len(turns) == len(rows), 'EVALUATION_HISTORY_VIEW_CHANGED')
                matches = [i for i, row in enumerate(rows[:-1]) if row.get('role') == 'user'
                    and type(row.get('text')) is str
                    and _text_sha(row['text']) == leaf['followup_prompt_sha256']
                    and row.get('attachments') == pngs
                    and rows[i+1].get('role') == 'assistant'
                    and rows[i+1].get('complete') is True
                    and rows[i+1].get('text') == reply['text']]
                need(len(matches) == 1, 'EVALUATION_HISTORICAL_TURN_NOT_READABLE_IN_ROOM')
                values = turns[matches[0]].evaluate(FILE_WITNESS,
                    {'names': csvs, 'bodySelector': native.USER_BODY})
                need(NativeDataFileDelivery._witness_ready(values, csvs),
                    'EVALUATION_HISTORICAL_FILES_NOT_READABLE_IN_ROOM')
                witnesses.append(dict(material_sha256=digest(material),
                    completion_sha256=material['delivery_custody']['native_completion_sha256'],
                    user_prompt_sha256=leaf['followup_prompt_sha256'],
                    assistant_sha256=reply['sha256'], attachment_names=names))
            room.verify_prefix(room.full_turns())
        finally:
            bridge.uploaded_names = previous_names
        result = digest(witnesses)
        need(self.custody is None or self.custody == result, 'EVALUATION_HISTORICAL_CUSTODY_CHANGED')
        self.custody = result
        return result

    def before_send(self, prompt, *, transcript):
        self.verify_material_access()
        room = self.owner.daily_room
        sequence = len(self.intents) + 1
        value = dict(schema_version=1, kind='NATIVE_POST_TRADE_EVALUATION',
            operation=self.payload['operation'], request_sha256=self.payload['request_sha256'],
            marker=self.payload['marker'], phase=PHASE,
            input_request_sha256=self.envelope['request_sha256'],
            input_evidence_sha256=self.request['input_evidence_sha256'],
            room_id=room.room_id, historical_access_sha256=self.custody,
            rendered_prompt_sha256=_text_sha(prompt), transcript_sha256=digest(transcript),
            conversation_url_sha256=hashlib.sha256(self.owner.bridge.page.url.encode()).hexdigest(),
            followup_sequence=sequence,
            previous_followup_intent_sha256=(__import__('agent.native_chart_followup', fromlist=('sha',)).sha(self.intents[-1])
                if self.intents else None))
        need(self.owner.followup_receipt(stage='INTENT', receipt=copy.deepcopy(value),
            deadline=self.deadline) is True, 'EVALUATION_SEND_INTENT_NOT_DURABLE')
        self.intents.append(value)

    def complete(self, result):
        from .native_chart_followup import sha
        need(self.intents and type(result) is dict and result.get('chart_evidence') == [],
            'EVALUATION_COMPLETION_UNVERIFIED')
        for intent in self.intents:
            receipt = dict(intent, intent_sha256=sha(intent), response_id=result['response_id'],
                final_reply_sha256=hashlib.sha256(result['assistant_text'].encode()).hexdigest())
            need(self.owner.followup_receipt(stage='COMPLETE', receipt=receipt,
                deadline=self.deadline) is True, 'EVALUATION_COMPLETION_NOT_DURABLE')
