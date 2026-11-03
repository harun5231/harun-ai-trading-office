"""Normal website navigation only: await an observed final main-frame 200."""
import asyncio
from types import SimpleNamespace
import unittest
from agent import native_chart_rpc as rpc

URL = 'https://www.binance.com/en/futures/BTCUSDT'


class Page:
    def __init__(self, final=None, initial=202):
        self.main_frame = object()
        self.url = URL
        self.listeners = []
        self.final = final
        self.initial = initial
        self.finished_observed = False
        self.loaded = False

    def on(self, name, callback):
        self.listeners.append((name, callback))

    def remove_listener(self, name, callback):
        self.listeners.remove((name, callback))

    def response(self, status, url=URL, main=True):
        return SimpleNamespace(status=status, url=url, request=SimpleNamespace(
            frame=self.main_frame if main else object(), is_navigation_request=lambda: True))

    async def wait_for_load_state(self, state, timeout):
        assert state == 'domcontentloaded' and timeout > 0
        assert self.finished_observed
        self.loaded = True

    async def goto(self, url, **kwargs):
        initial = self.response(self.initial)
        responses = [initial] + [self.response(*row) for row in (self.final or [])]
        for response in responses:
            for name, callback in list(self.listeners):
                if name == 'response':
                    callback(response)
            for name, callback in list(self.listeners):
                if name == 'requestfinished':
                    self.finished_observed = True
                    callback(response.request)
        return initial


class WebsiteNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_navigation_requires_observed_final_main_frame_200(self):
        page = Page([(200, URL, True)])
        result = await rpc.navigate_website(page, URL, timeout_ms=100)
        self.assertEqual(result, {'status': 200, 'url': URL})
        self.assertTrue(page.loaded)
        self.assertEqual(page.listeners, [])

    async def test_challenge_without_final_document_remains_blocked(self):
        page = Page()
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(rpc.navigate_website(page, URL, timeout_ms=20), .02)
        self.assertEqual(page.listeners, [])

    async def test_other_url_or_subframe_does_not_prove_navigation(self):
        for final in ([(200, URL.replace('BTCUSDT', 'ETHUSDT'), True)], [(200, URL, False)]):
            page = Page(final)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(rpc.navigate_website(page, URL, timeout_ms=20), .02)
            self.assertEqual(page.listeners, [])

    async def test_direct_200_preserves_existing_path_and_403_refuses(self):
        self.assertEqual(await rpc.navigate_website(Page(initial=200), URL, timeout_ms=100), {'status': 200, 'url': URL})
        page = Page(initial=403)
        with self.assertRaises(rpc.NativeChartRPCUnavailable):
            await rpc.navigate_website(page, URL, timeout_ms=100)
        self.assertEqual(page.listeners, [])


if __name__ == '__main__':
    unittest.main()
