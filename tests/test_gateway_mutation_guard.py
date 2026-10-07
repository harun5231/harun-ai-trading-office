import threading
import unittest

from worker.research_guard import (
    gateway_mutations, gateway_mutations_allowed, gateway_transaction_reads,
    research_reads, check_research_read, ResearchReadPaused,
)


class GatewayMutationGuardTests(unittest.TestCase):
    def test_standalone_gateway_is_not_authorized(self):
        self.assertFalse(gateway_mutations_allowed())

    def test_off_change_is_observed_without_reentering_scope(self):
        state = {'on': True}
        with gateway_mutations(lambda: state['on']):
            self.assertTrue(gateway_mutations_allowed())
            state['on'] = False
            self.assertFalse(gateway_mutations_allowed())
        self.assertFalse(gateway_mutations_allowed())

    def test_read_drain_does_not_authorize_financial_mutations(self):
        with research_reads(lambda: False), gateway_mutations(lambda: False):
            with self.assertRaises(ResearchReadPaused):
                check_research_read()
            with gateway_transaction_reads():
                check_research_read()
                self.assertFalse(gateway_mutations_allowed())
            with self.assertRaises(ResearchReadPaused):
                check_research_read()

    def test_nested_scope_cannot_broaden_permission(self):
        with gateway_mutations(lambda: False):
            with gateway_mutations(lambda: True):
                self.assertFalse(gateway_mutations_allowed())
        with gateway_mutations(lambda: True):
            with gateway_mutations(lambda: False):
                self.assertFalse(gateway_mutations_allowed())
            self.assertTrue(gateway_mutations_allowed())

    def test_predicate_failure_blocks_mutations(self):
        def broken():
            raise RuntimeError('unavailable settings')
        with gateway_mutations(broken):
            self.assertFalse(gateway_mutations_allowed())

    def test_predicate_must_return_literal_true(self):
        for value in (1, 'ON', None, False):
            with gateway_mutations(lambda: value):
                self.assertFalse(gateway_mutations_allowed())

    def test_scope_restores_permission_after_exception(self):
        with gateway_mutations(lambda: True):
            with self.assertRaises(RuntimeError):
                with gateway_mutations(lambda: False):
                    raise RuntimeError('operation failed')
            self.assertTrue(gateway_mutations_allowed())
        self.assertFalse(gateway_mutations_allowed())

    def test_actor_scope_does_not_authorize_another_thread(self):
        observed = []
        with gateway_mutations(lambda: True):
            thread = threading.Thread(target=lambda: observed.append(gateway_mutations_allowed()))
            thread.start()
            thread.join()
            self.assertTrue(gateway_mutations_allowed())
        self.assertEqual(observed, [False])

    def test_rejects_non_callable_scope(self):
        with self.assertRaises(TypeError):
            with gateway_mutations(True):
                pass


if __name__ == '__main__':
    unittest.main()
