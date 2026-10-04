"""Paper-only quote monitor. No broker calls. Quote source must match the ledger row."""
import json
from decimal import Decimal as D
from .core import Review, number

class PaperMonitor:
    def __init__(self,ledger): self.ledger=ledger
    def quote(self,trade_id,symbol,price,source):
        price=number(price)
        row=self.ledger.db.execute('SELECT * FROM trades WHERE id=?',(trade_id,)).fetchone()
        if not row: raise Review('NEEDS_REVIEW: order tidak ditemukan')
        plan=json.loads(row['plan'])
        if row['source']!=source or plan['symbol']!=symbol: raise Review('NEEDS_REVIEW: quote source/symbol salah')
        events=[]
        if row['state']=='CLOSED': return events
        if row['state']=='ORDER_READY':
            filled=price<=D(plan['entry']) if plan['side']=='LONG' else price>=D(plan['entry'])
            if not filled: return events
            self.ledger.fill(trade_id);events.extend(['POSITION_OPEN','MONITORING'])
        stop=price<=D(plan['sl']) if plan['side']=='LONG' else price>=D(plan['sl'])
        target=price>=D(plan['tp']) if plan['side']=='LONG' else price<=D(plan['tp'])
        if stop or target:
            # Sampled gap loss can exceed planned 5 USDT. TP credited only at target.
            self.ledger.close(trade_id,str(price if stop else D(plan['tp'])));events.append('CLOSED')
        return events
