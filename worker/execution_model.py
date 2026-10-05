"""Pure future-state contract, no network/executor. All evidence here is model-only."""
from dataclasses import dataclass,asdict,replace

class ModelError(Exception):pass
@dataclass(frozen=True)
class ExecutionModel:
    state: str = 'PLAN_READY'
    tp_confirmed: bool = False
    sl_confirmed: bool = False
    model_only: bool = True
    fill_confirmed: bool = False

    def __post_init__(self):
        if any(type(v) is not bool for v in (self.tp_confirmed,self.sl_confirmed,self.model_only,self.fill_confirmed)):raise ModelError('INVALID_MODEL_EVIDENCE')
        if self.state=='POSITION_PROTECTED' and not (self.tp_confirmed and self.sl_confirmed and self.fill_confirmed):raise ModelError('INVALID_MODEL_EVIDENCE')

    @property
    def blocks_next(self):
        return self.state not in ('PLAN_READY','POSITION_PROTECTED','CLOSED')

    def step(self,event):
        if not self.model_only:raise ModelError('LIVE_MODEL_DISABLED')
        transitions={
            ('PLAN_READY','ENTRY_ACK'):'ENTRY_SUBMITTED',
            ('PLAN_READY','ENTRY_UNCERTAIN'):'RECONCILIATION_REQUIRED',
            ('ENTRY_SUBMITTED','ENTRY_UNCERTAIN'):'RECONCILIATION_REQUIRED',
            ('ENTRY_SUBMITTED','CONFIRMED_FULL_FILL'):'ENTRY_CONFIRMED',
            ('ENTRY_SUBMITTED','PARTIAL_FILL'):'PROTECTION_INCOMPLETE',
            ('RECONCILIATION_REQUIRED','RECONCILED_FULL_FILL_SAME_ID'):'ENTRY_CONFIRMED',
            ('RECONCILIATION_REQUIRED','RECONCILED_OPEN_SAME_ID'):'ENTRY_SUBMITTED',
            ('PROTECTION_INCOMPLETE','RECONCILED_FILL_REMAINDER_CLEARED'):'ENTRY_CONFIRMED',
            ('ENTRY_CONFIRMED','PROTECTION_ACK_PENDING'):'PROTECTION_SUBMITTED',
            ('ENTRY_CONFIRMED','PROTECTION_FAILED'):'PROTECTION_INCOMPLETE',
            ('PROTECTION_SUBMITTED','PROTECTION_FAILED'):'PROTECTION_INCOMPLETE',
            ('POSITION_PROTECTED','PROTECTION_LOST'):'PROTECTION_INCOMPLETE',
            ('POSITION_PROTECTED','CONFIRMED_FLAT_AND_SIBLING_CLEARED'):'CLOSED',
            ('PROTECTION_INCOMPLETE','CONFIRMED_FLAT_AND_SIBLING_CLEARED'):'CLOSED',
        }
        if event in ('TP_CONFIRMED','SL_CONFIRMED') and self.state in ('PROTECTION_SUBMITTED','PROTECTION_INCOMPLETE'):
            if not self.fill_confirmed:raise ModelError('FILL_RECONCILIATION_REQUIRED')
            value=replace(self,tp_confirmed=self.tp_confirmed or event=='TP_CONFIRMED',sl_confirmed=self.sl_confirmed or event=='SL_CONFIRMED')
            return replace(value,state='POSITION_PROTECTED' if value.tp_confirmed and value.sl_confirmed else 'PROTECTION_INCOMPLETE')
        state=transitions.get((self.state,event))
        if state is None:raise ModelError('INVALID_MODEL_TRANSITION')
        return replace(self,state=state,tp_confirmed=False,sl_confirmed=False,fill_confirmed=state=='ENTRY_CONFIRMED' or (self.fill_confirmed and state not in ('RECONCILIATION_REQUIRED','CLOSED')))

    def data(self):return asdict(self)
