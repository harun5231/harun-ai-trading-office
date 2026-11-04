"""Historical material delivery inside the existing native DailyRoom operation.

Reuse genuinely readable original turns. Across days, upload exact archived
bytes through the existing attachment menu; never substitute a fresh chart or
change any live clock. Once intents precede uploads and each possible Send.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import re
from pathlib import Path

from .native_evaluation_protocol import envelope, PHASE, native_sha, validate_material_access_chain
from .native_file_upload import NativeDataFileDelivery, FILE_WITNESS
from worker.trade_evaluation_evidence import digest, need


def _text_sha(value):
    return hashlib.sha256(value.replace('\r\n', '\n').strip().encode()).hexdigest()


class HistoricalFileDelivery(NativeDataFileDelivery):
    """Only data selection changes; the existing PNG/CSV upload owns the UI."""
    def __init__(self, evaluation, gate):
        self.evaluation=evaluation
        owner=evaluation.owner
        super().__init__(owner.bridge,owner.native,gate,wall_clock=owner.wall_clock)

    def data_files(self,payload):
        return copy.deepcopy(self.evaluation.active_group()['csvs'])

    def _upload(self,composer,payload,artifacts):
        if not artifacts:
            return self.original_upload(composer,payload,artifacts)
        self.evaluation.before_upload()
        bound=super()._upload(composer,payload,artifacts)
        self.evaluation.uploaded(bound,list(self.bridge.uploaded_names))
        return bound


class HistoricalEvaluation:
    def __init__(self, owner, payload, deadline):
        self.owner,self.payload,self.deadline=owner,payload,deadline
        self.envelope=envelope(payload);self.request=self.envelope['request']
        room=owner.daily_room
        need(room is not None and room._connection is not None
            and callable(owner.followup_receipt),'EVALUATION_DAILY_ROOM_REQUIRED')
        selected=room._connection.execute('SELECT room_id,state FROM daily_rooms WHERE day=?',(room.day,)).fetchone()
        need(selected is not None and selected[0]==self.request['room_id'] and selected[1]=='BOUND',
            'EVALUATION_BOUND_ROOM_NOT_AVAILABLE')
        self.intents=[];self.completions={};self.last_ui=None
        self.covered={};self.reused=[];self.reused_indices=set();self.groups={}
        self.active=None;self.next_index=None;self.pending_send=None;self.bound=None
        self.sent_charts=[];self.rendered_prompts={};self._reused_recorded=False
        self.original_artifacts=owner.native.image_artifacts
        self.original_prompt=getattr(owner.native,'user_prompt',None)
        self.original_fresh=owner.bridge._fresh_conversation
        self._installed=False

    def _turn_witness(self,index):
        """Actual old user turn, decoded PNGs, CSV tiles and original reply."""
        from worker.trade_evaluation_archive import read_bound_material_turn
        material=self.request['historical_materials'][index]
        reply=self.request['source_neurobro_replies'][index]
        room=self.owner.daily_room;bridge=self.owner.bridge;native=self.owner.native
        receipt=read_bound_material_turn(room.store.root,material);leaf=receipt['intent']
        names=leaf['followup_attachment_names'];pngs=names[:2];csvs=names[2:]
        previous_names=bridge.uploaded_names
        try:
            bridge.uploaded_names=list(pngs);rows=room.full_turns();turns=room.original_visible(native.MESSAGE)
            need(len(turns)==len(rows),'EVALUATION_HISTORY_VIEW_CHANGED')
            matches=[i for i,row in enumerate(rows[:-1]) if row.get('role')=='user'
                and type(row.get('text')) is str and _text_sha(row['text'])==leaf['followup_prompt_sha256']
                and row.get('attachments')==pngs and rows[i+1].get('role')=='assistant'
                and rows[i+1].get('complete') is True and rows[i+1].get('text')==reply['text']]
            need(len(matches)==1,'EVALUATION_HISTORICAL_TURN_NOT_READABLE_IN_ROOM')
            values=turns[matches[0]].evaluate(FILE_WITNESS,{'names':csvs,'bodySelector':native.USER_BODY})
            need(NativeDataFileDelivery._witness_ready(values,csvs),
                'EVALUATION_HISTORICAL_FILES_NOT_READABLE_IN_ROOM')
            return dict(material_sha256=digest(material),
                native_completion_sha256=material['delivery_custody']['native_completion_sha256'],
                user_prompt_sha256=leaf['followup_prompt_sha256'],assistant_sha256=reply['sha256'],
                attachment_names=names,bound_charts=copy.deepcopy(leaf['followup_chart_evidence']))
        finally:
            bridge.uploaded_names=previous_names

    def prepare_room(self):
        """Use genuine DailyRoom restoration once, before selecting materials."""
        room=self.owner.daily_room
        composer=room.fresh()
        need(room.room_id==self.request['room_id'],'EVALUATION_BOUND_ROOM_NOT_AVAILABLE')
        for index,material in enumerate(self.request['historical_materials']):
            try:
                witness=self._turn_witness(index)
            except ValueError as error:
                # Custody/archive errors remain blockers. Only absence from the
                # current real room selects historical re-delivery.
                need(str(error) in {'EVALUATION_HISTORICAL_TURN_NOT_READABLE_IN_ROOM',
                    'EVALUATION_HISTORICAL_FILES_NOT_READABLE_IN_ROOM'},str(error))
            else:
                self.covered[index]=witness;self.reused.append(witness);self.reused_indices.add(index)
        missing=[i for i in range(len(self.request['historical_materials'])) if i not in self.covered]
        self.active=missing[0] if missing else None
        if self.active is not None:self.active_group()
        used=[False]
        def fresh_once():
            need(not used[0] and room._prepared and not composer.input_value(),
                'EVALUATION_COMPOSER_CHANGED')
            used[0]=True;return composer
        self._installed=True
        self.owner.bridge._fresh_conversation=fresh_once
        def artifacts(paths,evidence):
            if not paths and not evidence:return []
            for group in self.groups.values():
                if list(paths)==group['paths'] and list(evidence)==group['chart_evidence']:
                    return [(Path(row['path']),row['buffer'],copy.deepcopy(witness))
                        for row,witness in zip(group['pngs'],group['chart_evidence'])]
            raise self.owner.native.NotSubmitted('UNAVAILABLE')
        self.owner.native.image_artifacts=artifacts
        def prompt(payload):
            base=self.original_prompt(payload)
            return base+self.material_description(self.active) if self.active is not None else base
        self.owner.native.user_prompt=prompt
        self._installed=True
        return (list(self.active_group()['paths']),copy.deepcopy(self.active_group()['chart_evidence'])) if self.active is not None else ([],[])

    def active_group(self):
        need(self.active is not None,'EVALUATION_MATERIAL_NOT_SELECTED')
        if self.active not in self.groups:
            from worker.trade_evaluation_archive import read_bound_material_files
            name=getattr(self.owner.native,'__name__','browser_runtime.dom').split('.')[0]+'.charts'
            self.groups[self.active]=read_bound_material_files(self.owner.daily_room.store.root,
                self.request['historical_materials'][self.active],charts=importlib.import_module(name))
        return self.groups[self.active]

    def material_description(self,index):
        if index is None:return ''
        material=self.request['historical_materials'][index];group=self.groups[index]
        source=self.request['source_neurobro_replies'][index]
        return ('\n\nMateri HISTORIS transaksi '+source['trade_id']+' untuk '+group['symbol']+'. '
            'Ini rekaman bahan analisis semula, BUKAN kondisi pasar saat ini. '
            'Dua CSV masing-masing memuat 200 candle tertutup; dua PNG adalah screenshot asli saat itu. '
            'Waktu rekaman 15M: '+group['captured_at']['15m']+'; 1H: '+group['captured_at']['1h']+'. '
            'Jangan memakai bahan ini untuk entry baru.\nJawaban Neurobro semula untuk transaksi ini:\n'
            +source['text']+'\nTinjau bahan transaksi ini bersama hasil perdagangan yang sudah disertakan. '
            'Pisahkan fakta, ketidakpastian dan hipotesis; jangan mengaku eksperimen sudah dijalankan.')

    def _persist(self,action,**fields):
        room=self.owner.daily_room
        need(room.room_id==self.request['room_id'] and room._prepared,'EVALUATION_ROOM_CHANGED')
        value=dict(schema_version=1,kind='NATIVE_POST_TRADE_EVALUATION',operation=self.payload['operation'],
            request_sha256=self.payload['request_sha256'],marker=self.payload['marker'],phase=PHASE,
            input_request_sha256=self.envelope['request_sha256'],input_evidence_sha256=self.request['input_evidence_sha256'],
            room_id=room.room_id,action=action,
            response_id_kind=('NATIVE_FINAL_RESPONSE_ID' if action=='FINAL_ASSESSMENT' else 'NATIVE_UI_MESSAGE_ID'),
            historical_access_sha256=digest(self.request['historical_materials']),
            conversation_url_sha256=hashlib.sha256(self.owner.bridge.page.url.encode()).hexdigest(),
            followup_sequence=len(self.intents)+1,
            previous_followup_intent_sha256=native_sha(self.intents[-1]) if self.intents else None,
            reused_materials=copy.deepcopy(self.reused) if not self._reused_recorded else [],**fields)
        need(self.owner.followup_receipt(stage='INTENT',receipt=copy.deepcopy(value),deadline=self.deadline) is True,
            'EVALUATION_SEND_INTENT_NOT_DURABLE')
        self.intents.append(value);self._reused_recorded=True
        return value

    def before_upload(self):
        group=self.active_group();material=self.request['historical_materials'][self.active]
        need(self.active not in self.covered and self.bound is None,'EVALUATION_DUPLICATE_MATERIAL_UPLOAD')
        self._persist('PREPARE_MATERIAL',material_sha256=digest(material),
            planned_files=[{k:r[k] for k in ('kind','timeframe','sha256','bytes')} for r in material['files']])

    def uploaded(self,bound,names):
        group=self.active_group()
        need(type(bound) is list and len(bound)==2 and type(names) is list and len(names)==4,
            'EVALUATION_MATERIAL_UPLOAD_UNVERIFIED')
        for actual,expected in zip(bound,group['chart_evidence']):
            need({k:v for k,v in actual.items() if k!='attachment_id'}==expected
                and type(actual.get('attachment_id')) is str,'EVALUATION_MATERIAL_UPLOAD_UNVERIFIED')
        expected_names=[self.payload['marker']+'-'+r['timeframe']+'-'+r['sha256'][:12]+'.png' for r in bound]
        expected_names.extend(r['name'] for r in group['csvs'])
        need(names==expected_names,'EVALUATION_MATERIAL_NAMES_MISMATCH')
        self.bound=dict(charts=copy.deepcopy(bound),names=list(names))

    def before_send(self,prompt,*,transcript):
        need(self.pending_send is None,'EVALUATION_SEND_ALREADY_PENDING')
        room=self.owner.daily_room;room.verify_prefix(room.full_turns())
        for index in sorted(self.reused_indices):
            need(self._turn_witness(index)==self.covered[index],'EVALUATION_HISTORICAL_CUSTODY_CHANGED')
        if self.active is not None and self.active not in self.covered:
            need(self.bound is not None and self.intents and self.intents[-1]['action']=='PREPARE_MATERIAL',
                'EVALUATION_MATERIAL_UPLOAD_UNVERIFIED')
            material_sha=digest(self.request['historical_materials'][self.active])
            intent=self._persist('SEND_MATERIAL',material_sha256=material_sha,
                prepared_intent_sha256=native_sha(self.intents[-1]),
                bound_charts=copy.deepcopy(self.bound['charts']),attachment_names=list(self.bound['names']),
                rendered_prompt_sha256=_text_sha(prompt),transcript_sha256=digest(transcript))
        else:
            intent=self._persist('SEND_TEXT',material_sha256=None,bound_charts=[],attachment_names=[],
                rendered_prompt_sha256=_text_sha(prompt),transcript_sha256=digest(transcript))
        self.pending_send=intent

    def _complete_intent(self,intent,response_id,reply_sha):
        need(intent['followup_sequence'] not in self.completions,'EVALUATION_REPLY_RECEIPT_ALREADY_RECORDED')
        receipt=dict(intent,intent_sha256=native_sha(intent),response_id=response_id,final_reply_sha256=reply_sha)
        need(self.owner.followup_receipt(stage='COMPLETE',receipt=receipt,deadline=self.deadline) is True,
            'EVALUATION_COMPLETION_NOT_DURABLE')
        self.completions[intent['followup_sequence']]=receipt

    def note_reply(self,answer,turns):
        need(self.pending_send is not None and len(turns)>=2,'EVALUATION_MATERIAL_TURN_UNVERIFIED')
        user,reply=turns[-2:]
        need(type(user.get('text')) is str and _text_sha(user['text'])==self.pending_send['rendered_prompt_sha256']
            and user.get('attachments',[])==self.pending_send['attachment_names']
            and reply.get('text')==answer and reply.get('complete') is True
            and type(reply.get('id')) is str and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',reply['id']),
            'EVALUATION_MATERIAL_TURN_UNVERIFIED')
        reply_sha=hashlib.sha256(answer.encode()).hexdigest()
        if self.pending_send['action']=='SEND_MATERIAL':
            need(self.active not in self.covered,'EVALUATION_DUPLICATE_MATERIAL_UPLOAD')
            self.covered[self.active]=dict(material_sha256=self.pending_send['material_sha256'],
                user_prompt_sha256=self.pending_send['rendered_prompt_sha256'],
                attachment_names=self.pending_send['attachment_names'],assistant_sha256=reply_sha)
            self.sent_charts.extend(self.pending_send['bound_charts']);self.bound=None
            # PREPARE and SEND refer to this actual material turn, not the
            # final turn of a later group. Preserve both immutable records.
            prepared=self.intents[self.pending_send['followup_sequence']-2]
            self._complete_intent(prepared,reply['id'],reply_sha)
        self._complete_intent(self.pending_send,reply['id'],reply_sha)
        self.last_ui=dict(message_id=reply['id'],assistant_sha256=reply_sha)
        self.pending_send=None

    def next_material_question(self):
        missing=[i for i in range(len(self.request['historical_materials'])) if i not in self.covered]
        if not missing:return None
        self.next_index=missing[0]
        return ('Berikut rekaman asli untuk transaksi berikutnya dalam evaluasi yang sama. '
            'Teruskan pembahasan menggunakan konteks transaksi dan hasilnya; ini bukan setup baru.')

    def prepare_followup(self,question,composer,delivery,payload):
        if self.next_index is None:
            self.active=None;return question,[],None
        self.active=self.next_index;self.next_index=None;self.active_group()
        artifacts=self.owner.native.image_artifacts(self.active_group()['paths'],self.active_group()['chart_evidence'])
        bound=delivery._upload(composer,payload,artifacts)
        return question+self.material_description(self.active),list(self.owner.bridge.uploaded_names),bound

    def complete(self,result):
        need(self.pending_send is None and len(self.covered)==len(self.request['historical_materials'])
            and self.last_ui is not None
            and hashlib.sha256(result['assistant_text'].encode()).hexdigest()==self.last_ui['assistant_sha256'],
            'EVALUATION_HISTORICAL_MATERIAL_INCOMPLETE')
        charts=copy.deepcopy(self.sent_charts)
        prior=[dict(intent=value,complete=self.completions[value['followup_sequence']]) for value in self.intents]
        # Retain the ACTUAL final native result before returning over HTTP.
        # If that HTTP/worker handoff dies, recovery reads these exact bytes;
        # it must never reconstruct a reply from hashes or send it again.
        native_result={**{k:self.payload[k] for k in ('operation','marker','request_sha256')},
            **copy.deepcopy(result),'chart_evidence':charts}
        final=self._persist('FINAL_ASSESSMENT',source_chain_sha256=digest(prior),
            final_ui_message_id=self.last_ui['message_id'],final_ui_reply_sha256=self.last_ui['assistant_sha256'],
            native_result=native_result,native_received_at=self.owner.wall_clock())
        self._complete_intent(final,result['response_id'],self.last_ui['assistant_sha256'])
        chain=[dict(intent=value,complete=self.completions[value['followup_sequence']]) for value in self.intents]
        validate_material_access_chain(self.request,chain,charts,
            final_response=dict(response_id=result['response_id'],assistant_sha256=self.last_ui['assistant_sha256']))
        return charts

    def close(self):
        if self._installed:
            self.owner.native.image_artifacts=self.original_artifacts
            self.owner.native.user_prompt=self.original_prompt
            self.owner.bridge._fresh_conversation=self.original_fresh
