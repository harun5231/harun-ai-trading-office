"""Small immutable boundary values; authority remains with the backend bridge."""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping


class AgentError(RuntimeError):
    """A classified error safe to expose without its underlying exception text."""


class NotSent(AgentError):
    """An adapter proves the request was rejected before external submission."""


class RobotOff(NotSent):
    pass


class AmbiguousOperation(AgentError):
    pass


class BudgetExceeded(NotSent):
    pass


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    symbol: str
    created_at: float
    quantity: Decimal
    executed_quantity: Decimal
    revision: str
    owned: bool = True


@dataclass(frozen=True)
class AccountSnapshot:
    robot_on: bool
    observed_at: float
    generation: str
    jakarta_day: str
    balance_usdt: Decimal
    risk_usdt: Decimal
    daily_new_trades: int
    positions: tuple[Position, ...] = ()
    pending: tuple[PendingOrder, ...] = ()
    supported_symbols: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def occupied_slots(self) -> int:
        observed = len({p.symbol for p in self.positions if p.quantity != 0} |
                       {p.symbol for p in self.pending if p.quantity > p.executed_quantity})
        reconciled = self.metadata.get("reconciled_occupied_slots", observed)
        if type(reconciled) is not int or reconciled < observed:
            raise AgentError("RECONCILED_SLOT_COUNT_INVALID")
        return reconciled

    @property
    def available_slots(self) -> int:
        return max(0, min(2 - self.occupied_slots, 2 - self.daily_new_trades))


@dataclass(frozen=True)
class MarketContext:
    symbol: str
    observed_at: float
    data: Mapping[str, Any]
    screenshots: tuple[bytes, ...] = ()


@dataclass(frozen=True)
class NeurobroSetup:
    symbol: str
    side: str
    entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    source_message_id: str
    source_text: str
    source_sha256: str


@dataclass(frozen=True)
class DialogueDecision:
    action: str
    symbols: tuple[str, ...] = ()
    question: str = ""


@dataclass(frozen=True)
class TickResult:
    state: str
    phase: str
    reason: str = ""
