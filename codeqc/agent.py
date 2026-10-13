from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from time import perf_counter
from typing import Callable

import httpx
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.managed import RemainingSteps
from langgraph.prebuilt import ToolNode, tools_condition
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import AppConfig, ModelConfig


LOG = logging.getLogger(__name__)


TYPE_GUIDANCE = {
    "syntax": "Focus on syntax errors, type errors, invalid APIs, and defects that prevent correct execution.",
    "suggestion": "Focus on correctness, security, reliability, maintainability, performance, and test gaps.",
    "enhancement": "Focus on optional design, usability, observability, performance, and developer-experience improvements.",
    "all": "Perform a complete review: syntax, defects, security, reliability, maintainability, performance, tests, and enhancements.",
}


class ReviewState(MessagesState):
    remaining_steps: RemainingSteps


READ_ONLY_FILESYSTEM_TOOLS = {
    "read_text_file",
    "read_multiple_files",
    "list_directory",
    "list_directory_with_sizes",
    "directory_tree",
    "search_files",
    "get_file_info",
    "list_allowed_directories",
}


async def load_filesystem_tools(session: ClientSession):
    """Load only non-mutating tools advertised by the filesystem MCP server."""
    tools = await load_mcp_tools(session)
    selected = [tool for tool in tools if tool.name in READ_ONLY_FILESYSTEM_TOOLS]
    if not selected:
        advertised = ", ".join(sorted(tool.name for tool in tools)) or "none"
        raise RuntimeError(
            "Filesystem MCP server advertised no supported read-only tools "
            f"(advertised: {advertised})"
        )
    return selected


def _llm(model: ModelConfig) -> ChatOpenAI:
    key = model.api_key or "not-required"
    client = httpx.Client(verify=model.verify_ssl, timeout=model.timeout_seconds)
    async_client = httpx.AsyncClient(verify=model.verify_ssl, timeout=model.timeout_seconds)
    return ChatOpenAI(
        model=model.model_name,
        base_url=model.base_url,
        api_key=key,
        temperature=model.temperature,
        timeout=model.timeout_seconds,
        http_client=client,
        http_async_client=async_client,
    )


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
            else ""
            for block in content
        )
    return str(content)


def _reasoning_content(message) -> object | None:
    additional = getattr(message, "additional_kwargs", None) or {}
    response = getattr(message, "response_metadata", None) or {}
    return additional.get("reasoning_content") or response.get("reasoning_content")


def _has_reasoning_content(message) -> bool:
    additional = getattr(message, "additional_kwargs", None) or {}
    response = getattr(message, "response_metadata", None) or {}
    return "reasoning_content" in additional or "reasoning_content" in response


def _response_metrics(message, duration_seconds: float) -> dict[str, object]:
    metadata = getattr(message, "response_metadata", None) or {}
    usage = getattr(message, "usage_metadata", None) or metadata.get("token_usage") or {}
    content = _content_text(getattr(message, "content", ""))
    return {
        "duration_seconds": round(duration_seconds, 3),
        "finish_reason": metadata.get("finish_reason"),
        "token_usage": usage,
        "content_length": len(content),
        "tool_call_count": len(getattr(message, "tool_calls", None) or []),
        "has_reasoning_content": _has_reasoning_content(message),
    }


async def _invoke_logged(
    llm,
    messages,
    model: ModelConfig,
    phase: str,
    trace_callback: Callable[[str], None] | None,
):
    started = perf_counter()
    try:
        response = await asyncio.wait_for(
            llm.ainvoke(messages), timeout=model.call_timeout_seconds
        )
    except asyncio.TimeoutError:
        duration = perf_counter() - started
        LOG.error(
            "LLM call timed out model=%s phase=%s duration_seconds=%.3f deadline_seconds=%s",
            model.model_name, phase, duration, model.call_timeout_seconds,
        )
        raise TimeoutError(
            f"{phase} LLM call exceeded the {model.call_timeout_seconds}s deadline"
        ) from None
    metrics = _response_metrics(response, perf_counter() - started)
    LOG.info("LLM call model=%s phase=%s metrics=%s", model.model_name, phase, metrics)
    if trace_callback:
        trace_callback(
            f"llm {phase}: duration={metrics['duration_seconds']}s, "
            f"finish_reason={metrics['finish_reason']}, tokens={metrics['token_usage']}, "
            f"content_length={metrics['content_length']}, "
            f"tool_calls={metrics['tool_call_count']}, "
            f"reasoning_content={metrics['has_reasoning_content']}"
        )
    return response


def _trace_value(value: object, limit: int = 160) -> str:
    """Render a tool argument safely without making trace entries unbounded."""
    rendered = " ".join(str(value).split())
    if len(rendered) > limit:
        rendered = rendered[: limit - 1] + "…"
    return rendered


def _describe_tool_call(call: dict) -> str:
    name = str(call.get("name", "unknown"))
    args = call.get("args") or {}
    if not isinstance(args, dict):
        return name
    if name in {"read_file", "read_text_file"}:
        path = _trace_value(args.get("path", "?"))
        if name == "read_file":
            start = args.get("start_line", 1)
            end = args.get("end_line")
            end = "EOF" if end is None else end
            return f"read_file(path={path}, lines={start}-{end})"
        return f"read_text_file(path={path})"
    if name == "list_directory":
        return f"list_directory(path={_trace_value(args.get('path', '.'))})"
    if name == "list_workspace_files":
        return "list_workspace_files(path=.)"
    if name == "search_code":
        return f"search_code(query={_trace_value(args.get('query', ''))})"
    if not args:
        return name
    rendered_args = ", ".join(
        f"{key}={_trace_value(value)}" for key, value in sorted(args.items())
    )
    return f"{name}({rendered_args})"


async def run_review_async(
    config: AppConfig,
    model_name: str,
    review_type: str,
    user_prompt: str,
    root: Path,
    progress_callback: Callable[[str], None] | None = None,
    trace_callback: Callable[[str], None] | None = None,
) -> str:
    model = config.models[model_name]
    workspace = root.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError(f"Review workspace is not a directory: {workspace}")
    server = config.filesystem_mcp
    server_params = StdioServerParameters(
        command=server.command,
        args=[*server.args, str(workspace)],
    )
    if progress_callback:
        progress_callback("Starting the filesystem MCP server.")
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await load_filesystem_tools(session)
            if trace_callback:
                trace_callback(
                    "mcp: loaded read-only filesystem tools: "
                    + ", ".join(sorted(tool.name for tool in tools))
                )
            return await _run_review_graph(
                config, model, review_type, user_prompt, tools,
                progress_callback, trace_callback,
            )


async def _run_review_graph(
    config: AppConfig,
    model: ModelConfig,
    review_type: str,
    user_prompt: str,
    tools,
    progress_callback: Callable[[str], None] | None,
    trace_callback: Callable[[str], None] | None,
) -> str:
    base_llm = _llm(model)
    tool_llm = base_llm.bind_tools(tools)
    final_llm = (
        base_llm.bind(extra_body=model.final_extra_body)
        if model.final_extra_body else base_llm
    )
    request = (
        f"Review type: {review_type}\n{TYPE_GUIDANCE[review_type]}\n"
        "Use the tools to inspect the uploaded workspace. Do not ask the user questions. "
        "Return the final report in Markdown.\n"
    )
    if user_prompt:
        request += f"Additional user instructions:\n{user_prompt}"
    async def call_model(state: ReviewState):
        if state["remaining_steps"] <= 3:
            if progress_callback:
                progress_callback("Tool budget reached; preparing the final report.")
            final_request = HumanMessage(content=(
                "The workspace-inspection budget is exhausted. Do not call any more tools. "
                "Using only the evidence already collected in this conversation, produce the final "
                "Markdown code-quality report now. Clearly state any review limitations."
            ))
            response = await _invoke_logged(
                final_llm, [*state["messages"], final_request], model,
                "budget-final", trace_callback,
            )
        else:
            response = await _invoke_logged(
                tool_llm, state["messages"], model, "review", trace_callback
            )
        return {"messages": [response]}

    builder = StateGraph(ReviewState)
    builder.add_node("reviewer", call_model)
    builder.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    builder.add_edge(START, "reviewer")
    builder.add_conditional_edges("reviewer", tools_condition)
    builder.add_edge("tools", "reviewer")
    graph = builder.compile()

    initial_messages = [
        SystemMessage(content=config.default_system_prompt),
        HumanMessage(content=request),
    ]
    recursion_limit = config.limits.recursion_limit
    latest_state = None
    pending_tool_calls: dict[str, str] = {}
    async for mode, data in graph.astream(
        {"messages": initial_messages},
        config={"recursion_limit": recursion_limit},
        stream_mode=["values", "updates"],
    ):
        if mode == "values":
            latest_state = data
        elif mode == "updates" and trace_callback:
            for node_name, update in data.items():
                messages = update.get("messages", []) if isinstance(update, dict) else []
                last_message = messages[-1] if messages else None
                tool_calls = getattr(last_message, "tool_calls", None) or []
                if tool_calls:
                    descriptions = []
                    for call in tool_calls:
                        description = _describe_tool_call(call)
                        descriptions.append(description)
                        call_id = call.get("id")
                        if call_id:
                            pending_tool_calls[str(call_id)] = description
                    trace_callback(f"{node_name}: requested tool(s): {', '.join(descriptions)}")
                elif node_name == "tools":
                    completed = []
                    for message in messages:
                        call_id = str(getattr(message, "tool_call_id", "") or "")
                        description = pending_tool_calls.pop(call_id, None)
                        completed.append(
                            description or str(getattr(message, "name", None) or "tool")
                        )
                    trace_callback(f"tools: completed {', '.join(completed) or 'workspace inspection'}")
                else:
                    trace_callback(f"{node_name}: completed")
                if progress_callback:
                    progress_callback(f"LangGraph step completed: {node_name}.")
    if latest_state is None:
        raise RuntimeError("Review graph produced no state")
    final_message = latest_state["messages"][-1]
    if getattr(final_message, "tool_calls", None):
        raise RuntimeError("Review ended before the model produced a final report")
    synthesis_request = HumanMessage(content=(
        "Synthesize the final Markdown code-quality report now from the evidence and draft "
        "already present in this conversation. Do not call tools, do not describe your "
        "reasoning, and return only the visible final report."
    ))
    final_message = await _invoke_logged(
        final_llm, [*latest_state["messages"], synthesis_request], model,
        "final-synthesis", trace_callback,
    )
    content = _content_text(final_message.content).strip()
    if not content and _reasoning_content(final_message):
        retry_request = HumanMessage(content=(
            "Emit the final report now. Return only visible Markdown and do not think, explain, "
            "or call tools."
        ))
        final_message = await _invoke_logged(
            final_llm, [*latest_state["messages"], retry_request], model,
            "final-retry", trace_callback,
        )
        content = _content_text(final_message.content).strip()
    return content
