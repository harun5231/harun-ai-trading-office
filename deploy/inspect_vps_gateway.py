"""Bounded, static inspection of the running image; never import its gateway.

Run with Python's -I -B -S flags. The report is intentionally incomplete:
redacted source and fingerprints do not establish exchange order acceptance.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys


MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 64 * 1024
FLOW_FILES = (
    "robot.py", "robot_store.py", "account_state.py", "core.py",
    "analysis.py", "market.py", "binance_private.py", "binance_office.py",
    "neuroapi.py", "prompts.py", "provenance.py", "robot_provenance.py",
    "api_service.py", "research_guard.py",
)

# These are public protocol tokens, not a pattern matching arbitrary strings.
PUBLIC_STRINGS = frozenset({
    "", "https://fapi.binance.com", "https://fapi.binance.com/",
    "/fapi/v1/order", "/fapi/v1/openOrders", "/fapi/v1/allOrders",
    "/fapi/v1/algoOrder", "/fapi/v1/openAlgoOrders", "/fapi/v1/allAlgoOrders",
    "/fapi/v1/leverage", "/fapi/v1/marginType", "/fapi/v1/positionSide/dual",
    "/fapi/v1/time", "/fapi/v1/exchangeInfo", "/fapi/v1/ticker/price",
    "/fapi/v1/premiumIndex", "/fapi/v1/commissionRate",
    "/fapi/v1/symbolConfig", "/fapi/v1/leverageBracket",
    "/fapi/v2/account", "/fapi/v3/account", "/fapi/v2/positionRisk",
    "/fapi/v3/positionRisk", "/fapi/v1/apiTradingStatus",
    "GET", "POST", "PUT", "DELETE", "get", "post", "put", "delete",
    "&", "=", "?", "/", "&signature=", "utf-8", "ascii", "0", "true", "false",
    "LONG", "SHORT", "BUY", "SELL",
    "BOTH", "CROSS", "CROSSED", "ISOLATED", "75", "LIMIT", "MARKET",
    "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET",
    "CONDITIONAL", "GTC", "IOC", "FOK", "GTX", "MARK_PRICE", "CONTRACT_PRICE",
    "NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "CANCELLED",
    "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH", "PENDING", "SUBMITTING",
    "ENTRY_PENDING", "POSITION_PROTECTED", "PROTECTED", "CLOSED", "NEEDS_REVIEW", "UNKNOWN",
    "CONFIGURED", "NOT_CONNECTED", "CONNECTED", "BINANCE_NOT_CONFIGURED",
    "BINANCE_ORDER_GATEWAY_NOT_CONNECTED", "ORDER_INTENT",
    "BINANCE_API_KEY_FILE", "BINANCE_API_SECRET_FILE", "BINANCE_API_KEY",
    "BINANCE_API_SECRET", "BINANCE_FUTURES", "filled_quantity", "X-MBX-APIKEY",
    "api_key", "api_secret", "base_url", "connected", "status", "failure_code",
    "symbol", "side", "positionSide", "position_side", "type", "order_type",
    "quantity", "executedQty", "cumQty", "origQty", "cumQuote", "avgPrice",
    "price", "stopPrice", "triggerPrice", "timeInForce", "time_in_force",
    "reduceOnly", "closePosition", "workingType", "working_type", "priceProtect",
    "newClientOrderId", "clientOrderId", "origClientOrderId", "orderId",
    "responseType", "newOrderRespType", "maxNotionalValue", "initialLeverage",
    "notionalCap", "notionalFloor", "marginAsset", "availableBalance",
    "positionAmt", "positionRisk", "brackets", "symbolConfig",
    "newClientAlgoId", "clientAlgoId", "algoId", "algoType", "algoStatus",
    "timestamp", "recvWindow", "signature", "dualSidePosition", "marginType",
    "leverage", "margin_mode", "entry", "protection", "exit_side", "stop_loss",
    "take_profit", "intent_id", "client_order_id", "order_id", "entry_order_id",
    "sl_order_id", "tp_order_id", "entry_filled_qty", "first_fill_at",
    "sl_confirmed", "tp_confirmed", "closed_at", "exit_order_id",
    "entry_price", "entry_quantity", "position_quantity", "protected_quantity",
    "observed_at", "checked_at", "updateTime", "time", "transactTime", "serverTime",
    "code", "msg", "data", "orders", "positions", "order", "success",
    "receipt", "state", "mode", "sl", "tp", "execution_quantity",
    "risk_target_usdt", "risk", "risk_usdt", "gross_risk", "gross_risk_usdt",
    "entry_fee_usdt", "sl_exit_fee_usdt", "tp_exit_fee_usdt", "net_reward",
    "net_reward_usdt", "net_rr", "net_reward_risk", "fee_evidence", "source",
    "fee_source", "fee_symbol", "fee_observed_at", "taker_rate", "entry_fee_rate",
    "excluded_costs", "evidence_sha256", "provenance", "payload_sha256",
    "submit", "reconcile", "__func__",
})
# Keep a small explicit operational set; account IDs and arbitrary numbers vanish.
PUBLIC_INTEGERS = frozenset({0, 1, 2, 3, 5, 10, 15, 28, 30, 36, 60, 75, 100, 1000, 5000, 10000, 60000})
PUBLIC_BYTES = frozenset(text.encode("ascii") for text in PUBLIC_STRINGS)
REDACTED = "<REDACTED>"
REDACTED_IDENTIFIER = "_redacted_identifier"


class InspectionError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _read_regular(path, missing_ok=False):
    """Read fixed source paths only, without following a final symlink."""
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InspectionError("SOURCE_READ_FAILED")
        if info.st_size > MAX_SOURCE_BYTES:
            raise InspectionError("SOURCE_TOO_LARGE")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, MAX_SOURCE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise InspectionError("SOURCE_TOO_LARGE")
        return b"".join(chunks)
    except InspectionError:
        raise
    except FileNotFoundError:
        if missing_ok:
            return None
        raise InspectionError("SOURCE_READ_FAILED") from None
    except Exception:
        raise InspectionError("SOURCE_READ_FAILED") from None
    finally:
        if fd is not None:
            os.close(fd)


def _sql_authorizer(action, first, second, database, trigger):
    # Even a future accidental SQL change cannot read account/secret payloads.
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and first == "robot_settings" and second in {"id", "enabled"}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and first == "query_only" and second == "ON":
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def require_robot_off(data_dir):
    path = Path(data_dir) / "trading" / "ledger.sqlite3"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InspectionError("ROBOT_OFF_CHECK_FAILED")
        db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            db.set_authorizer(_sql_authorizer)
            db.execute("PRAGMA query_only=ON")
            row = db.execute("SELECT enabled FROM robot_settings WHERE id=1").fetchone()
        finally:
            db.close()
    except InspectionError:
        raise
    except Exception:
        raise InspectionError("ROBOT_OFF_CHECK_FAILED") from None
    if row == (1,):
        raise InspectionError("ROBOT_OFF_REQUIRED")
    if row != (0,):
        raise InspectionError("ROBOT_OFF_CHECK_FAILED")


class _RedactLiterals(ast.NodeTransformer):
    def generic_visit(self, node):
        # A pasted credential can also be valid Python syntax as an identifier.
        fields = {
            ast.Name: ("id",), ast.Attribute: ("attr",), ast.alias: ("name", "asname"),
            ast.FunctionDef: ("name",), ast.AsyncFunctionDef: ("name",),
            ast.ClassDef: ("name",), ast.arg: ("arg",), ast.keyword: ("arg",),
            ast.ExceptHandler: ("name",), ast.MatchAs: ("name",),
            ast.MatchStar: ("name",), ast.MatchMapping: ("rest",),
        }.get(type(node), ())
        for field in fields:
            value = getattr(node, field, None)
            if isinstance(value, str) and len(value) >= 48:
                setattr(node, field, REDACTED_IDENTIFIER)
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            node.names = [REDACTED_IDENTIFIER if len(name) >= 48 else name for name in node.names]
        return super().generic_visit(node)

    def visit_Constant(self, node):
        value = node.value
        if isinstance(value, str):
            safe = value in PUBLIC_STRINGS
        elif isinstance(value, bytes):
            safe = value in PUBLIC_BYTES
        elif value is None or value is Ellipsis or isinstance(value, bool):
            safe = True
        elif isinstance(value, int):
            safe = value in PUBLIC_INTEGERS
        else:
            safe = False
        if safe:
            return node
        redacted = REDACTED.encode("ascii") if isinstance(value, bytes) else REDACTED
        return ast.copy_location(ast.Constant(value=redacted), node)


def inspect(worker_root=Path("/app/worker"), data_dir=Path("/data")):
    require_robot_off(data_dir)
    root = Path(worker_root)
    source = _read_regular(root / "order_gateway.py")
    try:
        tree = ast.parse(source.decode("utf-8"))
        gateways = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OrderGateway"]
        if len(gateways) != 1:
            raise InspectionError("GATEWAY_CLASS_NOT_FOUND")
        tree = ast.fix_missing_locations(_RedactLiterals().visit(tree))
        functions = sorted(node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
        methods = sorted(node.name for node in gateways[0].body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
        sanitized = ast.unparse(tree)
    except InspectionError:
        raise
    except Exception:
        raise InspectionError("SOURCE_PARSE_FAILED") from None
    if len(sanitized.encode("utf-8")) > MAX_REPORT_BYTES:
        raise InspectionError("REPORT_TOO_LARGE")
    hashes, file_status = {}, {}
    for name in FLOW_FILES:
        raw = _read_regular(root / name, missing_ok=True)
        hashes[name] = hashlib.sha256(raw).hexdigest() if raw is not None else None
        file_status[name] = "PRESENT" if raw is not None else "MISSING"
    report = {
        "collector_version": 1,
        "robot_on": False,
        "gateway": {
            "sha256": hashlib.sha256(source).hexdigest(),
            "size_bytes": len(source),
            "module_functions": functions,
            "order_gateway_methods": methods,
            "sanitized_source": sanitized,
        },
        "workflow_sha256": hashes,
        "workflow_status": file_status,
    }
    encoded = json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise InspectionError("REPORT_TOO_LARGE")
    return encoded


def main(argv=None):
    parser = argparse.ArgumentParser(description="Static redacted gateway inspection; requires ROBOT OFF.")
    parser.add_argument("--worker-root", type=Path, default=Path("/app/worker"))
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    arguments = parser.parse_args(argv)
    try:
        result = inspect(arguments.worker_root, arguments.data_dir)
    except InspectionError as error:
        print(json.dumps({"error": error.code}, sort_keys=True))
        return 1
    except Exception:
        print('{"error": "GATEWAY_INSPECTION_FAILED"}')
        return 1
    sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
