"""The sole Binance order integration point, to be connected by the developer.

This build cannot submit, acknowledge, fill, or protect an exchange order. There
is no test/paper/manual executor fallback and no switch that enables submission.
"""
import hashlib
from .core import D, Review, number, validated_risk_target

NOT_CONNECTED = 'BINANCE_ORDER_GATEWAY_NOT_CONNECTED'


class GatewayUnavailable(Review):
    pass


def build_intent(plan, operation):
    """Immutable internal order command, never a user ticket or exchange receipt."""
    if not isinstance(plan, dict) or plan.get('mode') != 'ORDER_INTENT' or plan.get('symbol') == 'HYPEUSDT':
        raise Review('INVALID_ORDER_CONTRACT')
    try:
        target = validated_risk_target(plan['risk_target_usdt'])
        risk = number(plan['risk'])
    except (KeyError, TypeError, Review): raise Review('INVALID_ORDER_CONTRACT') from None
    if plan.get('side') not in ('LONG', 'SHORT') or not D('0') < risk <= target:
        raise Review('INVALID_ORDER_CONTRACT')
    entry_side = 'BUY' if plan['side'] == 'LONG' else 'SELL'
    client_id = 'hao-' + hashlib.sha256(operation.encode()).hexdigest()[:28]
    return dict(intent_id=operation, client_order_id=client_id,
        symbol=plan['symbol'], position_side='BOTH', side=plan['side'],
        entry=dict(order_type='LIMIT', side=entry_side, price=plan['entry'],
                   quantity=plan['execution_quantity'], time_in_force='GTC'),
        protection=dict(exit_side='SELL' if entry_side == 'BUY' else 'BUY',
                        stop_loss=plan['sl'], take_profit=plan['tp'], working_type='MARK_PRICE'),
        margin_mode=plan['margin_mode'], leverage=plan['leverage'],
        risk_target_usdt=plan['risk_target_usdt'], risk_usdt=plan['risk'],
        evidence_sha256=plan['provenance']['payload_sha256'])


class OrderGateway:
    connected = False

    def status(self):
        return dict(connected=self.connected,
                    status='CONNECTED' if self.connected else 'NOT_CONNECTED',
                    failure_code=None if self.connected else NOT_CONNECTED)

    def submit(self, intent):
        """Implement authenticated entry submission + real protection handling here.

        Return only a verified Binance observation in the contract documented in
        deploy/ORDER_INTEGRATION.md. Never synthesize an order/fill response.
        """
        raise GatewayUnavailable(NOT_CONNECTED)

    def reconcile(self, intent):
        """Read the real entry/protection lifecycle by its stable exchange IDs."""
        raise GatewayUnavailable(NOT_CONNECTED)
