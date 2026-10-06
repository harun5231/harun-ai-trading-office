"""Verified review tickets and deterministic offline lifecycle examples.

There is no transport, order executor, or persistence in this module. Rebuild a
ticket from its database-verified setup before each simulation. The ticket hash
detects changed JSON; it is not a replacement for provider/database provenance.
Every lifecycle event below is synthetic, including confirmations and fills.
"""
import copy
import re
from datetime import datetime, timezone
from fractions import Fraction

from .core import D, Review, number, validated_risk_target
from .execution_model import ExecutionModel
from .provenance import digest
from .robot_provenance import verify


SCENARIOS = frozenset((
    'FULL_TP', 'FULL_SL', 'PARTIAL_TP', 'PROTECTION_FAILURE', 'UNCERTAIN_ENTRY',
))
PNL_BASIS = 'EXCLUDES_FEES_FUNDING_SLIPPAGE'
_MANUAL_REVIEW_NOTE = ('Periksa ulang harga, filter quantity/harga, slot posisi, dan '
                       'open orders Binance sebelum mengirim secara manual.')
_TICKET_FIELDS = frozenset((
    'setup_id', 'symbol', 'side', 'status', 'execution_mode',
    'submission_enabled', 'review_required', 'margin_mode', 'leverage',
    'risk_target_usdt', 'risk', 'rr', 'quantity_step', 'entry', 'take_profit',
    'stop_loss', 'estimated_loss_usdt', 'estimated_profit_usdt', 'pnl_basis',
    'source', 'ticket_sha256', 'source_created_at', 'business_day', 'manual_review_note',
))


def _decimal(value):
    """Plain decimal text, with no float conversion or price/quantity rounding."""
    return format(value, 'f')


def _levels(ticket):
    """Validate the detached review contract without accepting live capabilities."""
    try:
        if not isinstance(ticket, dict) or set(ticket) != _TICKET_FIELDS:
            raise ValueError
        if ticket['ticket_sha256'] != digest({
            key: value for key, value in ticket.items() if key != 'ticket_sha256'
        }):
            raise ValueError
        if (ticket['execution_mode'] != 'MANUAL_ONLY'
                or ticket['submission_enabled'] is not False
                or ticket['review_required'] is not True
                or ticket['status'] not in ('SETUP_READY', 'APPROVED')
                or ticket['source'] != 'VERIFIED_NEUROAPI_V7'
                or ticket['pnl_basis'] != PNL_BASIS
                or ticket['margin_mode'] != 'CROSS'
                or type(ticket['leverage']) is not int or ticket['leverage'] != 75):
            raise ValueError
        symbol, side = ticket['symbol'], ticket['side']
        if (not isinstance(symbol, str)
                or not re.fullmatch(r'[A-Z0-9]{2,18}USDT', symbol)
                or symbol == 'HYPEUSDT' or side not in ('LONG', 'SHORT')):
            raise ValueError
        setup_id = ticket['setup_id']
        if (not isinstance(setup_id, str) or len(setup_id) > 160
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}:robot-v7:[0-2]:analysis-v7:'
                                    + re.escape(symbol), setup_id)):
            raise ValueError
        created = datetime.fromisoformat(ticket['source_created_at'])
        if (created.tzinfo is None or created.utcoffset().total_seconds() != 0
                or ticket['business_day'] != setup_id[:10]
                or ticket['manual_review_note'] != _MANUAL_REVIEW_NOTE):
            raise ValueError
        entry_order = ticket['entry']
        if not isinstance(entry_order, dict) or set(entry_order) != {
                'order_type', 'side', 'price', 'quantity', 'time_in_force', 'position_side'}:
            raise ValueError
        entry_side, exit_side = ('BUY', 'SELL') if side == 'LONG' else ('SELL', 'BUY')
        if (entry_order['order_type'], entry_order['side'], entry_order['time_in_force'],
                entry_order['position_side']) != ('LIMIT', entry_side, 'GTC', 'BOTH'):
            raise ValueError
        entry, quantity = number(entry_order['price']), number(entry_order['quantity'])
        prices = []
        for field, kind in (('take_profit', 'TAKE_PROFIT_MARKET'), ('stop_loss', 'STOP_MARKET')):
            order = ticket[field]
            if not isinstance(order, dict) or set(order) != {
                    'order_type', 'side', 'trigger_price', 'working_type', 'close_position'}:
                raise ValueError
            if ((order['order_type'], order['side'], order['working_type'])
                    != (kind, exit_side, 'MARK_PRICE') or order['close_position'] is not True):
                raise ValueError
            prices.append(number(order['trigger_price']))
        tp, sl = prices
        if not (sl < entry < tp if side == 'LONG' else tp < entry < sl):
            raise ValueError
        target, risk = validated_risk_target(ticket['risk_target_usdt']), number(ticket['risk'])
        loss, profit = quantity * abs(entry - sl), quantity * abs(tp - entry)
        step = number(ticket['quantity_step'])
        if (quantity % step or risk != loss or not 0 < risk <= target
                or number(ticket['estimated_loss_usdt']) != loss
                or number(ticket['estimated_profit_usdt']) != profit):
            raise ValueError
        from .binance_shadow import verified_rr
        verified_rr(dict(entry=str(entry), tp=str(tp), sl=str(sl), rr=ticket['rr']))
        return entry, tp, sl, quantity, step
    except Exception:
        raise Review('INVALID_MANUAL_TICKET') from None


def build_ticket(db, plan, setup_id, status):
    """Read existing v7 evidence and return a detached, non-executable review ticket."""
    if status not in ('SETUP_READY', 'APPROVED'):
        raise Review('SETUP_NOT_READY')
    if not isinstance(plan, dict):
        raise Review('ROBOT_SETUP_UNVERIFIED')
    if plan.get('symbol') == 'HYPEUSDT':
        raise Review('ROBOT_MANUAL_EXPOSURE_PROTECTED')
    verify(db, plan, setup_id)
    try:
        created = db.execute('SELECT created FROM api_requests WHERE operation=?',
                             (setup_id,)).fetchone()[0]
        if type(created) not in (int, float) or created < 0:
            raise ValueError
        source_created_at = datetime.fromtimestamp(created, timezone.utc).isoformat()
    except Exception:
        raise Review('ROBOT_SETUP_UNVERIFIED') from None
    entry, tp, sl, quantity = (number(plan[field]) for field in
                              ('entry', 'tp', 'sl', 'execution_quantity'))
    entry_side, exit_side = ('BUY', 'SELL') if plan['side'] == 'LONG' else ('SELL', 'BUY')
    ticket = dict(
        setup_id=setup_id, symbol=plan['symbol'], side=plan['side'], status=status,
        execution_mode='MANUAL_ONLY', submission_enabled=False, review_required=True,
        margin_mode=plan['margin_mode'], leverage=plan['leverage'],
        risk_target_usdt=plan['risk_target_usdt'], risk=plan['risk'], rr=plan['rr'],
        quantity_step=plan['sizing_rules']['step'],
        entry=dict(order_type='LIMIT', side=entry_side, price=plan['entry'],
                   quantity=plan['execution_quantity'], time_in_force='GTC', position_side='BOTH'),
        take_profit=dict(order_type='TAKE_PROFIT_MARKET', side=exit_side,
                         trigger_price=plan['tp'], working_type='MARK_PRICE', close_position=True),
        stop_loss=dict(order_type='STOP_MARKET', side=exit_side,
                       trigger_price=plan['sl'], working_type='MARK_PRICE', close_position=True),
        estimated_loss_usdt=_decimal(quantity * abs(entry - sl)),
        estimated_profit_usdt=_decimal(quantity * abs(tp - entry)),
        pnl_basis=PNL_BASIS, source='VERIFIED_NEUROAPI_V7',
        source_created_at=source_created_at, business_day=setup_id[:10],
        manual_review_note=_MANUAL_REVIEW_NOTE,
    )
    ticket['ticket_sha256'] = digest(ticket)
    _levels(ticket)
    return copy.deepcopy(ticket)


def simulate_ticket(ticket, scenario):
    """Run a deterministic model on the supplied levels, never a Binance request."""
    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        raise Review('INVALID_SIMULATION_SCENARIO')
    ticket = copy.deepcopy(ticket)
    entry, tp, sl, quantity, step = _levels(ticket)
    model = ExecutionModel()
    steps = []
    filled = D('0')
    remaining = quantity
    canceled = D('0')
    pnl = None

    def record(event, detail):
        steps.append(dict(
            sequence=len(steps) + 1, event=event, state=model.state,
            model_only=True, synthetic=True, fill_confirmed=model.fill_confirmed,
            sl_confirmed=model.sl_confirmed, tp_confirmed=model.tp_confirmed,
            filled_quantity=_decimal(filled) if filled is not None else None,
            remaining_quantity=_decimal(remaining) if remaining is not None else None,
            detail=detail,
        ))

    def advance(event, detail):
        nonlocal model
        model = model.step(event)
        record(event, detail)

    record('PLAN_READY', 'Model lokal menggunakan level tiket; tidak ada order dikirim.')
    if scenario == 'UNCERTAIN_ENTRY':
        filled = remaining = None
        advance('ENTRY_UNCERTAIN',
                'Hasil entry dimodelkan tidak diketahui; perlu rekonsiliasi, tanpa kirim ulang otomatis.')
    else:
        advance('ENTRY_ACK', 'Acknowledgement sintetis belum membuktikan entry terisi.')
        if scenario == 'PARTIAL_TP':
            # A legal lot-size multiple avoids pretending an impossible half-lot filled.
            half_steps = Fraction(quantity) // (2 * Fraction(step))
            filled = D(half_steps) * step
            if not 0 < filled < quantity:
                raise Review('SIMULATION_PARTIAL_FILL_UNAVAILABLE')
            remaining = quantity - filled
            advance('PARTIAL_FILL', 'Hanya sebagian quantity dimodelkan terisi.')
            canceled, remaining = remaining, D('0')
            record('REMAINDER_CANCEL_CONFIRMED', 'Sisa entry dibatalkan dalam model sebelum proteksi.')
            advance('RECONCILED_FILL_REMAINDER_CLEARED',
                    'Fill parsial dan pembatalan sisa direkonsiliasi secara sintetis.')
        else:
            filled, remaining = quantity, D('0')
            advance('CONFIRMED_FULL_FILL', 'Seluruh quantity entry dimodelkan terisi.')
        advance('PROTECTION_ACK_PENDING', 'Proteksi masih menunggu bukti SL dan TP sintetis.')
        advance('SL_CONFIRMED', 'SL sintetis dikonfirmasi lebih dahulu; TP masih diperlukan.')
        if scenario == 'PROTECTION_FAILURE':
            record('TP_PROTECTION_FAILED',
                   'TP gagal dalam model; proteksi belum lengkap dan setup berikutnya diblokir.')
        else:
            advance('TP_CONFIRMED', 'SL dan TP sintetis lengkap setelah fill dikonfirmasi.')
            exit_price = sl if scenario == 'FULL_SL' else tp
            record('SL_TRIGGERED' if scenario == 'FULL_SL' else 'TP_TRIGGERED',
                   'Exit pada level tiket dimodelkan; tidak memakai harga pasar langsung.')
            direction = 1 if ticket['side'] == 'LONG' else -1
            pnl = (exit_price - entry) * filled * direction
            advance('CONFIRMED_FLAT_AND_SIBLING_CLEARED',
                    'Posisi flat dan proteksi pasangan dibersihkan dalam model.')
    return dict(
        mode='SIMULATION', execution_mode='MANUAL_ONLY', scenario=scenario,
        setup_id=ticket['setup_id'], symbol=ticket['symbol'],
        real_order_submitted=False, live_execution=False, synthetic_trace=True,
        status=model.state, blocks_next=model.blocks_next,
        filled_quantity=_decimal(filled) if filled is not None else None,
        sample_fill_quantity=_decimal(filled) if filled is not None else None,
        remaining_quantity=_decimal(remaining) if remaining is not None else None,
        canceled_quantity=_decimal(canceled),
        pnl_usdt=_decimal(pnl) if pnl is not None else None, pnl_basis=PNL_BASIS,
        steps=steps,
    )
