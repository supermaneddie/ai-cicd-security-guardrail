import asyncio
import unittest
from unittest.mock import Mock

from langchain_core.messages import AIMessage

from codeqc.agent import _invoke_logged, _response_metrics
from codeqc.config import ModelConfig


class FakeLLM:
    def __init__(self, response=None, delay=0):
        self.response = response
        self.delay = delay

    async def ainvoke(self, _messages):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.response


class AgentCallTests(unittest.TestCase):
    def test_response_metrics_include_requested_diagnostics(self):
        response = AIMessage(
            content="report",
            tool_calls=[{"name": "read_text_file", "args": {}, "id": "1"}],
            additional_kwargs={"reasoning_content": "internal"},
            response_metadata={"finish_reason": "tool_calls", "token_usage": {"total_tokens": 9}},
        )
        metrics = _response_metrics(response, 1.23456)
        self.assertEqual(metrics["duration_seconds"], 1.235)
        self.assertEqual(metrics["finish_reason"], "tool_calls")
        self.assertEqual(metrics["token_usage"], {"total_tokens": 9})
        self.assertEqual(metrics["content_length"], 6)
        self.assertEqual(metrics["tool_call_count"], 1)
        self.assertTrue(metrics["has_reasoning_content"])

        empty_reasoning = AIMessage(content="", additional_kwargs={"reasoning_content": ""})
        self.assertTrue(_response_metrics(empty_reasoning, 0)["has_reasoning_content"])

    def test_invoke_logged_enforces_per_call_deadline(self):
        model = ModelConfig(
            model_name="test", base_url="http://example.invalid/v1", call_timeout_seconds=0.01
        )
        with self.assertRaisesRegex(TimeoutError, "deadline"):
            asyncio.run(_invoke_logged(FakeLLM(delay=0.05), [], model, "test", None))

    def test_invoke_logged_emits_trace_metrics(self):
        trace = Mock()
        response = AIMessage(content="done", response_metadata={"finish_reason": "stop"})
        model = ModelConfig(model_name="test", base_url="http://example.invalid/v1")
        result = asyncio.run(_invoke_logged(FakeLLM(response), [], model, "test", trace))
        self.assertIs(result, response)
        trace.assert_called_once()
        self.assertIn("finish_reason=stop", trace.call_args.args[0])
        self.assertIn("content_length=4", trace.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
