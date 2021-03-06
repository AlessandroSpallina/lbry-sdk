import asyncio

import lbry
import lbry.wallet
from lbry.error import ServerPaymentFeeAboveMaxAllowedError
from lbry.wallet.network import ClientSession
from lbry.testcase import IntegrationTestCase, CommandTestCase
from lbry.wallet.orchstr8.node import SPVNode


class TestSessions(IntegrationTestCase):
    """
    Tests that server cleans up stale connections after session timeout and client times out too.
    """

    LEDGER = lbry.wallet

    async def test_session_bloat_from_socket_timeout(self):
        await self.conductor.stop_spv()
        await self.ledger.stop()
        self.conductor.spv_node.session_timeout = 1
        await self.conductor.start_spv()
        session = ClientSession(
            network=None, server=(self.conductor.spv_node.hostname, self.conductor.spv_node.port), timeout=0.2
        )
        await session.create_connection()
        await session.send_request('server.banner', ())
        self.assertEqual(len(self.conductor.spv_node.server.session_mgr.sessions), 1)
        self.assertFalse(session.is_closing())
        await asyncio.sleep(1.1)
        with self.assertRaises(asyncio.TimeoutError):
            await session.send_request('server.banner', ())
        self.assertTrue(session.is_closing())
        self.assertEqual(len(self.conductor.spv_node.server.session_mgr.sessions), 0)

    async def test_proper_version(self):
        info = await self.ledger.network.get_server_features()
        self.assertEqual(lbry.__version__, info['server_version'])

    async def test_client_errors(self):
        # Goal is ensuring thsoe are raised and not trapped accidentally
        with self.assertRaisesRegex(Exception, 'not a valid address'):
            await self.ledger.network.get_history('of the world')
        with self.assertRaisesRegex(Exception, 'rejected by network rules.*TX decode failed'):
            await self.ledger.network.broadcast('13370042004200')


class TestUsagePayment(CommandTestCase):
    async def test_single_server_payment(self):
        wallet_pay_service = self.daemon.component_manager.get_component('wallet_server_payments')
        wallet_pay_service.payment_period = 1
        # only starts with a positive max key fee
        wallet_pay_service.max_fee = "0.0"
        await wallet_pay_service.start(ledger=self.ledger, wallet=self.wallet)
        self.assertFalse(wallet_pay_service.running)
        wallet_pay_service.max_fee = "1.0"
        await wallet_pay_service.start(ledger=self.ledger, wallet=self.wallet)
        self.assertTrue(wallet_pay_service.running)
        await wallet_pay_service.stop()
        await wallet_pay_service.start(ledger=self.ledger, wallet=self.wallet)

        address = await self.blockchain.get_raw_change_address()
        _, history = await self.ledger.get_local_status_and_history(address)
        self.assertEqual(history, [])

        node = SPVNode(self.conductor.spv_module, node_number=2)
        await node.start(self.blockchain, extraconf={"PAYMENT_ADDRESS": address, "DAILY_FEE": "1.1"})
        self.addCleanup(node.stop)
        self.daemon.jsonrpc_settings_set('lbryum_servers', [f"{node.hostname}:{node.port}"])
        await self.daemon.jsonrpc_wallet_reconnect()

        features = await self.ledger.network.get_server_features()
        self.assertEqual(features["payment_address"], address)
        self.assertEqual(features["daily_fee"], "1.1")
        with self.assertRaises(ServerPaymentFeeAboveMaxAllowedError):
            await asyncio.wait_for(wallet_pay_service.on_payment.first, timeout=8)

        await node.stop(False)
        await node.start(self.blockchain, extraconf={"PAYMENT_ADDRESS": address, "DAILY_FEE": "1.0"})
        self.daemon.jsonrpc_settings_set('lbryum_servers', [f"{node.hostname}:{node.port}"])
        await self.daemon.jsonrpc_wallet_reconnect()
        features = await self.ledger.network.get_server_features()
        self.assertEqual(features["payment_address"], address)
        self.assertEqual(features["daily_fee"], "1.0")
        tx = await asyncio.wait_for(wallet_pay_service.on_payment.first, timeout=8)
        self.assertIsNotNone(await self.blockchain.get_raw_transaction(tx.id))  # verify its broadcasted
        self.assertEqual(tx.outputs[0].amount, 100000000)
        self.assertEqual(tx.outputs[0].get_address(self.ledger), address)
