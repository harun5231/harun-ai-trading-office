"""The sole Binance order integration point, to be connected by the developer.

This build cannot submit, acknowledge, fill, or protect an exchange order. There
is no test/paper/manual executor fallback and no switch that enables submission.
"""
import hashlib
from .core import D, Review, number, validated_risk_target, validated_protection_working_type

NOT_CONNECTED = 'BINANCE_ORDER_GATEWAY_NOT_CONNECTED'


class GatewayUnavailable(Review):
    pass


class _MissingOrderImplementation:
    """Local placeholders only; implement the public OrderGateway below."""
    def submit(self, intent):
        raise GatewayUnavailable(NOT_CONNECTED)

    def reconcile(self, intent):
        raise GatewayUnavailable(NOT_CONNECTED)


def require_implementation(gateway, *names):
    """Require real adapter methods, independently of connection display data.

    This checks Python methods only and performs no I/O. An implemented adapter
    owns signing, configuration, exchange checks, and protection handling.
    Detect missing methods before claiming SUBMITTING: the built-in stubs can
    never have sent a request. Every exception after that claim remains unknown.
    """
    for name in names or ('submit', 'reconcile'):
        method = getattr(gateway, name, None)
        if not callable(method) or getattr(method, '__func__', method) is getattr(_MissingOrderImplementation, name):
            raise GatewayUnavailable(NOT_CONNECTED)


def build_intent(plan, operation):
    """Immutable internal order command, never a user ticket or exchange receipt."""
    from .core import validated_protection_working_type
    if not isinstance(plan, dict) or plan.get('mode') != 'ORDER_INTENT' or plan.get('symbol') == 'HYPEUSDT':
        raise Review('INVALID_ORDER_CONTRACT')
    try:
        target = validated_risk_target(plan['risk_target_usdt'])
        risk = number(plan['risk'])
        working_type = validated_protection_working_type(plan.get('protection_working_type', 'MARK_PRICE'))
    except (KeyError, TypeError, Review): raise Review('INVALID_ORDER_CONTRACT') from None
    if plan.get('side') not in ('LONG', 'SHORT') or not D('0') < risk <= target:
        raise Review('INVALID_ORDER_CONTRACT')
    entry_side = 'BUY' if plan['side'] == 'LONG' else 'SELL'
    client_id = 'hao-' + hashlib.sha256(operation.encode()).hexdigest()[:28]
    intent = dict(intent_id=operation, client_order_id=client_id,
        symbol=plan['symbol'], position_side='BOTH', side=plan['side'],
        entry=dict(order_type='LIMIT', side=entry_side, price=plan['entry'],
                   quantity=plan['execution_quantity'], time_in_force='GTC'),
        protection=dict(exit_side='SELL' if entry_side == 'BUY' else 'BUY',
                        stop_loss=plan['sl'], take_profit=plan['tp'], working_type=working_type),
        margin_mode=plan['margin_mode'], leverage=plan['leverage'],
        risk_target_usdt=plan['risk_target_usdt'], risk_usdt=plan['risk'],
        gross_risk_usdt=plan['gross_risk'],entry_fee_usdt=plan['entry_fee_usdt'],
        sl_exit_fee_usdt=plan['sl_exit_fee_usdt'],tp_exit_fee_usdt=plan['tp_exit_fee_usdt'],
        net_reward_usdt=plan['net_reward'],net_reward_risk=plan['net_rr'],
        fee_evidence=dict(source=plan['fee_source'],symbol=plan['fee_symbol'],
            observed_at=plan['fee_observed_at'],taker_rate=plan['entry_fee_rate']),
        excluded_costs=plan['excluded_costs'],
        evidence_sha256=plan['provenance']['payload_sha256'])
    if 'reward_risk_policy' in plan:
        from .core import Rules, validate_reward_risk_policy
        from dataclasses import fields
        try:
            sizing = plan['sizing_rules']
            if not isinstance(sizing, dict) or set(sizing) != {field.name for field in fields(Rules)}:
                raise ValueError
            rules = Rules(**{key: (D(value) if isinstance(value, str) and
                key not in ('fee_source', 'fee_symbol') else value) for key, value in sizing.items()})
            validate_reward_risk_policy(plan, rules)
            intent['reward_risk_policy'] = plan['reward_risk_policy']
            intent['tp_tick_size'] = format(number(rules.tick), 'f')
        except Exception:
            raise Review('INVALID_ORDER_CONTRACT') from None
    return intent


class OrderGateway(_MissingOrderImplementation):
    """Implement submit(intent), reconcile(intent), and status() in this class.

    submit must authenticate the entry and handle real SL/TP protection.
    reconcile must read its real lifecycle using stable exchange IDs.
    Return only verified observations per deploy/ORDER_INTEGRATION.md.
    """
    def status(self):
        """Display actual adapter status; never an execution permission switch.

        The built-in adapter has no transport. Its developer implementation
        must also report its actual configuration/connection status here.
        """
        return dict(connected=False, status='NOT_CONNECTED', failure_code=NOT_CONNECTED)
