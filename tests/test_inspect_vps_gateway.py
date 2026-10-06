"""Offline checks: the collector reads source, never imports/executes its SDK."""
import ast
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "inspect_vps_gateway.py"
SPEC = importlib.util.spec_from_file_location("vps_gateway_inspection", SCRIPT)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
SECRET = "SYNTHETIC_API_SECRET_817239042_NEVER_PRINT"
API_KEY = "SYNTHETIC_API_KEY_639184205_NEVER_PRINT"


class InspectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gateway-inspection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.worker = self.root / "worker"
        self.worker.mkdir()
        self.data = self.root / "data"
        (self.data / "trading").mkdir(parents=True)
        self.ledger = self.data / "trading" / "ledger.sqlite3"
        db = sqlite3.connect(self.ledger)
        db.execute("CREATE TABLE robot_settings(id INTEGER PRIMARY KEY,enabled INTEGER)")
        db.execute("INSERT INTO robot_settings VALUES(1,0)")
        db.execute("CREATE TABLE sensitive_payload(payload TEXT)")
        db.execute("INSERT INTO sensitive_payload VALUES(?)", (SECRET,))
        db.commit()
        db.close()
        for name in tool.FLOW_FILES:
            (self.worker / name).write_text("# offline workflow fixture\n", encoding="utf-8")
        self.gateway = self.worker / "order_gateway.py"
        self.marker = self.root / "IMPORTED_SDK"
        self.source = (
            f"# {SECRET}\n"
            "import requests\n"
            f"raise RuntimeError({API_KEY!r})\n"
            f"open({str(self.marker)!r}, 'w').write('BAD')\n"
            "def build_intent(plan, operation): return plan\n"
            "def require_implementation(gateway): return gateway\n"
            "class OrderGateway:\n"
            f"    '''{SECRET}'''\n"
            f"    api_key = {API_KEY!r}\n"
            f"    api_secret = {SECRET!r}\n"
            f"    binary_secret = {SECRET.encode()!r}\n"
            "    account_id = 639184205\n"
            "    leverage = 75\n"
            "    arbitrary_ratio = 0.123456789\n"
            "    url = 'https://fapi.binance.com'\n"
            "    secret_file = 'BINANCE_API_SECRET_FILE'\n"
            "    def submit(self, intent):\n"
            "        return {'method':'POST', 'path':'/fapi/v1/order',\n"
            "                'positionSide':'BOTH', 'side':'BUY', 'type':'LIMIT',\n"
            "                'marginType':'CROSSED', 'leverage':75, 'quantity':2}\n"
            "    verification_fields = {'sl_confirmed':True, 'tp_confirmed':True,\n"
            "        'closed_at':None, 'exit_order_id':None, 'newOrderRespType':'responseType',\n"
            "        'maxNotionalValue':'initialLeverage', 'notionalCap':'notionalFloor',\n"
            "        'marginAsset':'availableBalance', 'positionAmt':'positionRisk',\n"
            "        'brackets':'symbolConfig'}\n"
            "    config_paths = ['/fapi/v1/symbolConfig', '/fapi/v1/leverageBracket']\n"
            "    signing_tokens = ['get', 'post', 'put', 'delete', '&', '=', '?', 'utf-8', 'ascii']\n"
            "    def reconcile(self, intent): return {'source':'BINANCE_FUTURES', 'filled_quantity':'quantity', 'status':'FILLED'}\n"
            "    def status(self): return {'connected':True, 'status':'CONFIGURED'}\n"
        )
        self.gateway.write_text(self.source, encoding="utf-8")

    def invoke(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = tool.main(["--worker-root", str(self.worker), "--data-dir", str(self.data)])
        self.assertEqual(err.getvalue(), "")
        output = out.getvalue()
        self.assertNotIn(SECRET, output)
        self.assertNotIn(API_KEY, output)
        self.assertNotIn(str(self.marker), output)
        self.assertFalse(self.marker.exists())
        return code, json.loads(output), output

    def change_enabled(self, value):
        with sqlite3.connect(self.ledger) as db:
            db.execute("UPDATE robot_settings SET enabled=?", (value,))

    def test_static_redacted_report_and_public_order_contract(self):
        before = self.ledger.read_bytes()
        code, report, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertFalse(report["robot_on"])
        gateway = report["gateway"]
        self.assertEqual(gateway["sha256"], hashlib.sha256(self.source.encode()).hexdigest())
        self.assertEqual(gateway["module_functions"], ["build_intent", "require_implementation"])
        self.assertEqual(gateway["order_gateway_methods"], ["reconcile", "status", "submit"])
        sanitized = gateway["sanitized_source"]
        self.assertNotIn("639184205", sanitized)
        self.assertNotIn("0.123456789", sanitized)
        self.assertIn("<REDACTED>", sanitized)
        self.assertNotIn("#", sanitized)
        for token in ("https://fapi.binance.com", "/fapi/v1/order", "POST", "BOTH", "BUY", "LIMIT", "CROSSED", "BINANCE_API_SECRET_FILE"):
            self.assertIn(token, sanitized)
        self.assertIn("leverage = 75", sanitized)
        self.assertIn("'source': 'BINANCE_FUTURES'", sanitized)
        self.assertIn("'filled_quantity': 'quantity'", sanitized)
        for token in ("sl_confirmed", "tp_confirmed", "closed_at", "exit_order_id",
                      "responseType", "newOrderRespType", "maxNotionalValue", "initialLeverage",
                      "notionalCap", "notionalFloor", "marginAsset", "availableBalance",
                      "positionAmt", "positionRisk", "brackets", "symbolConfig",
                      "/fapi/v1/symbolConfig", "/fapi/v1/leverageBracket", "utf-8", "ascii"):
            self.assertIn(repr(token), sanitized)
        for token in ("get", "post", "put", "delete", "&", "=", "?"):
            self.assertIn(repr(token), sanitized)
        ast.parse(sanitized)
        self.assertEqual(set(report["workflow_sha256"]), set(tool.FLOW_FILES))
        self.assertLessEqual(len(output.encode()), tool.MAX_REPORT_BYTES)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(set(report), {"collector_version", "robot_on", "gateway", "workflow_sha256", "workflow_status"})
        self.assertEqual(set(report["workflow_status"].values()), {"PRESENT"})

    def test_sdk_is_never_imported_and_collector_has_only_stdlib_imports(self):
        original_import = __import__
        def guarded_import(name, *args, **kwargs):
            if name.startswith(("worker", "requests", "urllib", "http")):
                self.fail("collector attempted SDK/service/HTTP import")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=guarded_import):
            self.assertEqual(self.invoke()[0], 0)
        tree = ast.parse(SCRIPT.read_text())
        imports = {node.name.split('.')[0] for statement in tree.body if isinstance(statement, ast.Import) for node in statement.names}
        self.assertEqual(imports, {"argparse", "ast", "hashlib", "json", "os", "sqlite3", "stat", "sys"})
        from_imports = [node.module for node in tree.body if isinstance(node, ast.ImportFrom)]
        self.assertEqual(from_imports, ["pathlib"])

    def test_robot_on_refused_before_source_read(self):
        self.change_enabled(1)
        with patch.object(tool, "_read_regular", side_effect=AssertionError("source must not be read")):
            code, report, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(report, {"error": "ROBOT_OFF_REQUIRED"})

    def test_missing_setting_and_noncanonical_enabled_refused(self):
        for value in (None, 2, "not-off"):
            with self.subTest(value=value):
                self.change_enabled(value)
                self.assertEqual(self.invoke()[1], {"error": "ROBOT_OFF_CHECK_FAILED"})
        with sqlite3.connect(self.ledger) as db:
            db.execute("DELETE FROM robot_settings")
        self.assertEqual(self.invoke()[1], {"error": "ROBOT_OFF_CHECK_FAILED"})

    def test_missing_database_not_created(self):
        self.ledger.unlink()
        self.assertEqual(self.invoke()[1], {"error": "ROBOT_OFF_CHECK_FAILED"})
        self.assertFalse(self.ledger.exists())

    def test_database_read_failure_sanitized(self):
        with patch.object(tool.sqlite3, "connect", side_effect=sqlite3.OperationalError(SECRET)):
            code, report, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(report, {"error": "ROBOT_OFF_CHECK_FAILED"})

    def test_readonly_sql_is_only_off_query_and_authorizer_denies_mutation(self):
        connect = sqlite3.connect
        calls, trace = [], []
        def tracked_connect(*args, **kwargs):
            calls.append((args, kwargs))
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(trace.append)
            return connection
        with patch.object(tool.sqlite3, "connect", side_effect=tracked_connect):
            self.assertEqual(self.invoke()[0], 0)
        self.assertTrue(calls[0][0][0].endswith("?mode=ro"))
        self.assertEqual(calls[0][1], {"uri": True, "timeout": 5})
        self.assertEqual(trace, ["PRAGMA query_only=ON", "SELECT enabled FROM robot_settings WHERE id=1"])
        with connect(self.ledger) as connection:
            connection.set_authorizer(tool._sql_authorizer)
            for sql in ("UPDATE robot_settings SET enabled=1", "SELECT payload FROM sensitive_payload", "CREATE TABLE mutation(x)", "ATTACH DATABASE ':memory:' AS other"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(sql)

    def test_missing_gateway_refused_but_missing_workflow_files_are_reported(self):
        self.gateway.unlink()
        self.assertEqual(self.invoke()[1], {"error": "SOURCE_READ_FAILED"})
        self.gateway.write_text(self.source)
        for name in (tool.FLOW_FILES[0], "research_guard.py"):
            (self.worker / name).unlink()
        code, report, _ = self.invoke()
        self.assertEqual(code, 0)
        for name in (tool.FLOW_FILES[0], "research_guard.py"):
            self.assertIsNone(report["workflow_sha256"][name])
            self.assertEqual(report["workflow_status"][name], "MISSING")

    def test_source_and_database_symlinks_not_followed(self):
        actual = self.root / "private-source"
        self.gateway.rename(actual)
        self.gateway.symlink_to(actual)
        self.assertEqual(self.invoke()[1], {"error": "SOURCE_READ_FAILED"})
        self.gateway.unlink()
        actual.rename(self.gateway)
        other = self.root / "private-ledger"
        self.ledger.rename(other)
        self.ledger.symlink_to(other)
        self.assertEqual(self.invoke()[1], {"error": "ROBOT_OFF_CHECK_FAILED"})

    def test_source_hardlink_refused(self):
        (self.root / "alias").hardlink_to(self.gateway)
        self.assertEqual(self.invoke()[1], {"error": "SOURCE_READ_FAILED"})

    def test_parse_and_encoding_errors_never_echo_source(self):
        for data in (f"{SECRET} = (".encode(), b"\xff\xfe"):
            with self.subTest(data=data):
                self.gateway.write_bytes(data)
                self.assertEqual(self.invoke()[1], {"error": "SOURCE_PARSE_FAILED"})

    def test_missing_or_duplicate_ordergateway_is_refused(self):
        for source in (f"private = {SECRET!r}", "class OrderGateway: pass\nclass OrderGateway: pass\n"):
            self.gateway.write_text(source)
            self.assertEqual(self.invoke()[1], {"error": "GATEWAY_CLASS_NOT_FOUND"})

    def test_input_and_output_limits_refuse_without_partial_source(self):
        self.gateway.write_bytes(b"#" + b"x" * tool.MAX_SOURCE_BYTES)
        self.assertEqual(self.invoke()[1], {"error": "SOURCE_TOO_LARGE"})
        self.gateway.write_text("class OrderGateway:\n" + "    operational_constant = 75\n" * 4000)
        self.assertEqual(self.invoke()[1], {"error": "REPORT_TOO_LARGE"})

    def test_fstrings_and_nested_literals_redacted_bytes_preserved_only_when_public(self):
        self.gateway.write_text(
            "class OrderGateway:\n"
            f"    nested = {{'quantity': [{SECRET!r}, b'POST', 639184205]}}\n"
            f"    message = f'private-{SECRET}-{{75}}'\n"
        )
        code, report, _ = self.invoke()
        self.assertEqual(code, 0)
        source = report["gateway"]["sanitized_source"]
        self.assertIn("b'POST'", source)
        self.assertNotIn("639184205", source)
        self.assertNotIn("private-", source)
        ast.parse(source)

    def test_credential_length_identifiers_and_metadata_are_redacted(self):
        token = "SYNTHETIC_IDENTIFIER_CREDENTIAL_" + "X" * 40
        self.gateway.write_text(
            f"import {token} as {token}\n"
            f"def {token}({token}): return {token}\n"
            "class OrderGateway:\n"
            f"    def {token}(self): return self.{token}\n"
        )
        code, report, output = self.invoke()
        self.assertEqual(code, 0)
        self.assertNotIn(token, output)
        self.assertEqual(report["gateway"]["module_functions"], [tool.REDACTED_IDENTIFIER])
        self.assertEqual(report["gateway"]["order_gateway_methods"], [tool.REDACTED_IDENTIFIER])
        ast.parse(report["gateway"]["sanitized_source"])

    def test_exact_receipt_abi_signing_and_operational_values_are_visible(self):
        receipt_fields = (
            "source", "state", "symbol", "client_order_id", "order_id", "filled_quantity",
            "observed_at", "first_fill_at", "sl_confirmed", "tp_confirmed", "sl_order_id",
            "tp_order_id", "exit_order_id", "closed_at",
        )
        source = (
            "class OrderGateway:\n"
            "    def __init__(self, base_url='https://fapi.binance.com/'):\n"
            "        self.base_url = base_url.rstrip('/')\n"
            "    source = 'BINANCE_FUTURES'\n"
            "    state = 'POSITION_PROTECTED'\n"
            "    receipt_keys = " + repr(receipt_fields) + "\n"
            "    signing = ['&signature=', '0', 'true', 'false', 'serverTime']\n"
            "    operations = [3, 10, 28, 36, 1000]\n"
            "    close = {'closePosition': 'true'}\n"
            "    def submit(self, intent): return str(intent)[3:28:36]\n"
        )
        self.gateway.write_text(source)
        code, report, _ = self.invoke()
        self.assertEqual(code, 0)
        rendered = report["gateway"]["sanitized_source"]
        for field in receipt_fields:
            self.assertIn(repr(field), rendered)
        for token in ("BINANCE_FUTURES", "POSITION_PROTECTED", "https://fapi.binance.com/",
                      "/", "&signature=", "0", "true", "false", "serverTime"):
            self.assertIn(repr(token), rendered)
        self.assertIn("[3, 10, 28, 36, 1000]", rendered)
        self.assertIn("[3:28:36]", rendered)
        self.assertNotIn("<REDACTED>", rendered)

    def test_unknown_and_credential_bearing_urls_still_redact(self):
        for url in (f"https://fapi.binance.com/{SECRET}", f"https://{API_KEY}@fapi.binance.com/", "https://unknown.example.invalid/"):
            with self.subTest(url=url):
                self.gateway.write_text("class OrderGateway:\n    base_url = " + repr(url) + "\n")
                code, report, output = self.invoke()
                self.assertEqual(code, 0)
                self.assertNotIn(url, output)
                self.assertIn("<REDACTED>", report["gateway"]["sanitized_source"])


if __name__ == "__main__":
    unittest.main()
