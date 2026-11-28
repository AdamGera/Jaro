import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import unittest
from unittest.mock import AsyncMock, Mock, patch

from local_qwen import BASE_CONTEXT, LINUX_BIN_SHA, MAX_CONTEXT, LocalQwen, runtime_installed
from llama_loopback import listening_port


TITLE = {"type": "object", "properties": {"title": {"type": "string"}}}


class RuntimeTests(unittest.TestCase):
    def test_default_linux_binary_requires_the_pinned_install_receipt(self):
        with TemporaryDirectory() as folder:
            server = Path(folder) / "llama-server"
            server.touch()
            with patch("local_qwen.SERVER", server), patch("local_qwen.SERVER_OVERRIDE", None), patch("sys.platform", "linux"):
                self.assertFalse(runtime_installed())
                receipt = server.with_name(".verified")
                receipt.write_text("older runtime")
                self.assertFalse(runtime_installed())
                receipt.write_text(LINUX_BIN_SHA + "\n")
                self.assertTrue(runtime_installed())


class WorkerAddressTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_only_the_worker_loopback_address_and_a_valid_port(self):
        output = asyncio.StreamReader()
        output.feed_data(b"srv: listening on http://0.0.0.0:9000\nsrv: listening on http://127.0.0.1:99999\n")
        output.feed_data(b"srv: listening on http://127.0.0.1:43651\r\n")
        output.feed_eof()
        self.assertEqual(await listening_port(output), 43651)
        with self.assertRaisesRegex(RuntimeError, "before opening"):
            await listening_port(output)


class StructuredProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_unfinished_strings_stream_and_only_valid_results_are_cached(self):
        snapshots = []
        model = object.__new__(LocalQwen)
        with patch("writing_cache.recall", return_value=None), patch("writing_cache.remember") as save:
            async def generate(*args, **kwargs):
                yield '{"title": "مع'
                self.assertEqual(snapshots, [{"title": "مع"}])
                save.assert_not_called()
                await asyncio.sleep(0.07)
                yield 'رض'
                self.assertEqual(snapshots[-1], {"title": "معرض"})
                save.assert_not_called()
                yield ' الميناء"}'

            model.generate = generate
            result = await model.structured("Title", {}, TITLE, on_partial=snapshots.append)
            self.assertEqual(result, {"title": "معرض الميناء"})
            self.assertEqual(result, snapshots[-1])
            self.assertEqual(save.call_args.args[1], result)
            save.assert_called_once()

    async def test_retry_clears_previous_partial_and_hits_deliver_only_the_valid_result(self):
        model = object.__new__(LocalQwen)
        attempts = iter(['{"title":"Wrong', '{"title":"Correct"}'])
        snapshots = []

        async def generate(*args, **kwargs):
            yield next(attempts)

        model.generate = generate
        with patch("writing_cache.recall", return_value=None), patch("writing_cache.remember") as save:
            result = await model.structured("Title", {}, TITLE, on_partial=snapshots.append)
            self.assertEqual(snapshots[:2], [{"title": "Wrong"}, {}])
            self.assertEqual(snapshots[-1], {"title": "Correct"})
            save.assert_called_once()

        snapshots.clear()
        with patch("writing_cache.recall", return_value=result), patch("writing_cache.remember") as save:
            self.assertEqual(await model.structured("Title", {}, TITLE, on_partial=snapshots.append), result)
            self.assertEqual(snapshots, [result])
            save.assert_not_called()

    async def test_structured_stream_requires_normal_stop_even_for_parseable_json(self):
        for first_stop in ("length", None):
            with self.subTest(first_stop=first_stop):
                budgets = []
                def respond(request):
                    budgets.append(json.loads(request.content)["max_tokens"])
                    first = len(budgets) == 1
                    item = {"delta": {"content": json.dumps({"title": "Cut short" if first else "Full result"})},
                            "finish_reason": first_stop if first else "stop"}
                    sse = "data: " + json.dumps({"choices": [item]}) + "\n\ndata: [DONE]\n\n"
                    return httpx.Response(200, text=sse)
                async with httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(respond)) as http:
                    model = object.__new__(LocalQwen)
                    model.http = http
                    with patch("writing_cache.recall", return_value=None), patch("writing_cache.remember") as save:
                        result = await model.structured("Title", {}, TITLE, limit=3000)
                self.assertEqual(result, {"title": "Full result"})
                self.assertEqual(budgets, [3000, 4024])
                save.assert_called_once()
                self.assertEqual(save.call_args.args[1], result)

    async def test_terminal_error_clears_draft_without_caching(self):
        async def generate(*args, **kwargs):
            yield '{"title":"unfinished'

        model = object.__new__(LocalQwen)
        model.generate = generate
        snapshots = []
        with patch("writing_cache.recall", return_value=None), patch("writing_cache.remember") as save:
            with self.assertRaises(ValueError):
                await model.structured("Title", {}, TITLE, on_partial=snapshots.append)
            self.assertEqual(snapshots[-1], {})
            save.assert_not_called()

class FullContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_editorial_prompt_is_the_actual_user_message(self):
        model = object.__new__(LocalQwen)
        model.fit_prompt = AsyncMock()
        prompt = "Explain the disagreement first, then what resolved it."
        transcript = "First passage. Middle passage. Closing passage."

        async def generate(messages, **kwargs):
            self.assertEqual(messages[-1], {"role": "user", "content": prompt})
            self.assertIn(transcript, messages[1]["content"])
            self.assertIn("Editorial policy", messages[0]["content"])
            yield '{"title":"Resolution"}'

        model.generate = generate
        with patch("writing_cache.recall", return_value=None), patch("writing_cache.remember"):
            await model.structured(prompt, transcript, TITLE, policy="Editorial policy", full_context=True)
        model.fit_prompt.assert_awaited_once()

    async def test_sizes_the_templated_request_and_rejects_over_limit_without_truncation(self):
        model = object.__new__(LocalQwen)
        model.context_size = BASE_CONTEXT
        messages = [{"role": "user", "content": "all passages and the complete guide"}]
        response = Mock()
        response.json.return_value = {"prompt": "rendered with chat template"}
        model.http = AsyncMock()
        model.http.post.return_value = response
        model.tokens = AsyncMock(return_value=BASE_CONTEXT - 100)
        model.stop_process = AsyncMock()
        model.ready = AsyncMock()

        await model.fit_prompt(messages, 1000)
        self.assertEqual(model.context_size, BASE_CONTEXT * 2)
        self.assertEqual(model.http.post.call_args.kwargs["json"]["messages"], messages)
        model.tokens.assert_awaited_with("rendered with chat template")
        model.tokens.return_value = MAX_CONTEXT
        with self.assertRaisesRegex(RuntimeError, "full transcript"):
            await model.fit_prompt(messages, 1000)
        model.ready.assert_awaited_once()


class IdleStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_maybe_stop_idle_stops_only_when_idle_and_unbusy(self):
        model = object.__new__(LocalQwen)
        model.lock = asyncio.Lock()
        model.process = SimpleNamespace(returncode=None)
        model.audio_users = 0
        model.last_used = 10.0
        model.stop_process = AsyncMock()

        with patch("local_qwen.time.monotonic", return_value=200.0):
            self.assertFalse(await model.maybe_stop_idle(idle_seconds=300.0))
        model.stop_process.assert_not_awaited()

        with patch("local_qwen.time.monotonic", return_value=400.0):
            self.assertFalse(await model.maybe_stop_idle(idle_seconds=300.0, busy=True))
        model.stop_process.assert_not_awaited()

        async with model.lock:
            with patch("local_qwen.time.monotonic", return_value=400.0):
                self.assertFalse(await model.maybe_stop_idle(idle_seconds=300.0))
        model.stop_process.assert_not_awaited()

        with patch("local_qwen.time.monotonic", return_value=400.0):
            self.assertTrue(await model.maybe_stop_idle(idle_seconds=300.0))
        model.stop_process.assert_awaited_once()
