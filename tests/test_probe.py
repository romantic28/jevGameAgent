import asyncio
import json
import socket
import ssl
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from pydantic import ValidationError

from jev_game_agent.config import Settings
from jev_game_agent.contracts import ProbeRequest, default_case
from jev_game_agent.decisions.typesafe import JevClient, classify_error, retry_after_seconds
from jev_game_agent.server import create_server


FAKE_KEY = "p0-test-key-do-not-log"


def response_payload(choice="close_notice"):
    return {
        "model": "jev-test-version", "usage": {"input_tokens": 31, "output_tokens": 5},
        "answers": {"next_operation": {
            "type": "choice", "choice": choice, "confidence": 0.8,
            "probabilities": {"close_notice": 0.8, "wait": 0.1, "inspect_notice": 0.1},
        }},
    }


async def test_ten_calls_reuse_one_client_and_close_lifespan():
    requests = []
    created = []

    def handler(request):
        requests.append(request)
        assert request.url == "https://api.typesafe.ai/v1/systemone"
        assert FAKE_KEY not in request.content.decode()
        assert json.loads(request.content)["questions"]["next_operation"]["type"] == "choice"
        return httpx2.Response(200, json=response_payload())

    def factory(settings):
        client = JevClient(settings, transport=httpx2.MockTransport(handler))
        created.append(client)
        return client

    server = create_server(Settings(api_key=FAKE_KEY), client_factory=factory)
    async with Client(server, mode="legacy") as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {"start", "advance", "resume", "status", "finish"}
        assert "request" in tools.tools[0].input_schema["properties"]
        for count in (0, 10, 1):
            result = await client.call_tool("start", {"request": {"repetitions": count}})
            assert not result.is_error
            data = result.structured_content
            assert data["client_initializations"] == 1
            assert data["successful_repetitions"] == count
            assert not data["p0_accepted"] and not data["gameplay_enabled"]
            assert FAKE_KEY not in json.dumps(data)
    assert len(created) == 1 and len(requests) == 11
    assert created[0].sdk._http_client.is_closed


async def test_missing_key_is_structured_and_does_not_call_factory():
    def forbidden(settings):
        raise AssertionError("No client should be created without a key")

    async with Client(create_server(Settings(), client_factory=forbidden), mode="legacy") as client:
        result = await client.call_tool("start", {"request": {"repetitions": 10}})
        assert not result.is_error
        assert result.structured_content["error"]["category"] == "missing_key"
        assert result.structured_content["completed_repetitions"] == 0


@pytest.mark.parametrize("probe_input", [
    {"repetitions": 11}, {"repetitions": -1}, {"repetitions": True},
    {"repetitions": "10"}, {"mode": "gameplay"}, {"api_key": FAKE_KEY},
    {"text_case": {"state": "x", "instructions": "y", "criteria": {"": "bad", "valid": "x"}}},
])
def test_rejects_invalid_probe_input(probe_input):
    with pytest.raises(ValidationError):
        ProbeRequest.model_validate(probe_input)


@pytest.mark.parametrize("status, category, attempts", [
    (401, "authentication", 1), (403, "permission_denied", 1),
    (400, "request_format", 1), (422, "request_format", 1),
    (429, "rate_limit", 2), (503, "service", 2),
])
async def test_errors_are_classified_and_retry_is_bounded(status, category, attempts, caplog):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx2.Response(status, json={"message": FAKE_KEY})

    client = JevClient(Settings(api_key=FAKE_KEY), transport=httpx2.MockTransport(handler))
    try:
        sample = await client.evaluate(default_case(), 1)
    finally:
        await client.aclose()
    assert sample.error.category == category
    assert sample.attempts == count == attempts
    assert FAKE_KEY not in sample.model_dump_json()
    assert FAKE_KEY not in caplog.text


async def test_transient_failure_recovers_once():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx2.Response(503) if count == 1 else httpx2.Response(200, json=response_payload())

    client = JevClient(Settings(api_key=FAKE_KEY), transport=httpx2.MockTransport(handler))
    try:
        sample = await client.evaluate(default_case(), 1)
        assert sample.error is None and sample.attempts == 2
    finally:
        await client.aclose()


async def test_retry_after_outside_budget_does_not_sleep_or_retry():
    client = JevClient(Settings(api_key=FAKE_KEY), transport=httpx2.MockTransport(
        lambda request: httpx2.Response(429, headers={"Retry-After": "60"}),
    ))
    try:
        sample = await client.evaluate(default_case(), 1)
        assert sample.attempts == 1
        assert sample.error.retry_after_seconds == 60
    finally:
        await client.aclose()


async def test_overall_deadline_cancels_slow_request():
    async def handler(request):
        await asyncio.sleep(1)
        return httpx2.Response(200, json=response_payload())

    client = JevClient(Settings(api_key=FAKE_KEY, request_budget_seconds=0.03),
                       transport=httpx2.MockTransport(handler))
    try:
        sample = await client.evaluate(default_case(), 1)
        assert sample.error.category == "timeout" and sample.elapsed_ms < 500
    finally:
        await client.aclose()


@pytest.mark.parametrize("change", [
    {"choice": "unlisted"}, {"confidence": float("nan")},
    {"probabilities": {"close_notice": 0.8, "wait": 0.1}},
    {"probabilities": {"close_notice": 1.1, "wait": -0.2, "inspect_notice": 0.1}},
])
async def test_invalid_answers_never_pass(change):
    payload = response_payload()
    payload["answers"]["next_operation"].update(change)
    # JSON cannot encode NaN on the wire; use a string accepted by SDK float parsing.
    if "confidence" in change:
        payload["answers"]["next_operation"]["confidence"] = "NaN"
    client = JevClient(Settings(api_key=FAKE_KEY), transport=httpx2.MockTransport(
        lambda request: httpx2.Response(200, json=payload),
    ))
    try:
        sample = await client.evaluate(default_case(), 1)
        assert sample.error.category == "response_invalid" and sample.attempts == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize("cause, category", [
    (PermissionError("secret"), "network_permission"),
    (socket.gaierror(socket.EAI_NONAME, "secret"), "dns"),
    (ssl.SSLError("secret"), "tls"),
    (httpx2.ProxyError("secret"), "proxy"),
])
def test_network_cause_classification_does_not_expose_messages(cause, category):
    error = Exception("secret")
    error.__cause__ = cause
    classified = classify_error(error)
    assert classified.category == category
    assert "secret" not in classified.model_dump_json()


def test_settings_repr_does_not_include_key():
    assert FAKE_KEY not in repr(Settings(api_key=FAKE_KEY))
    assert retry_after_seconds({"Retry-After": "NaN"}) is None


def test_real_stdio_only_emits_protocol_json():
    project = Path(__file__).resolve().parents[1]
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "p0-wire-test", "version": "1"},
        }},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "start", "arguments": {"request": {"repetitions": 0}},
        }},
    ]
    import os
    env = dict(os.environ, TYPESAFE_API_KEY="")
    process = subprocess.Popen(
        [sys.executable, "-m", "jev_game_agent"], cwd=project, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    replies = []
    try:
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            if "id" in message:
                replies.append(json.loads(process.stdout.readline()))
        process.stdin.close()
        process.wait(timeout=10)
        extra_output = process.stdout.read()
        replies.extend(json.loads(line) for line in extra_output.splitlines() if line.strip())
        assert process.returncode == 0, process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    assert {reply.get("id") for reply in replies} >= {1, 2, 3}
    assert all(reply.get("jsonrpc") == "2.0" for reply in replies)
    check = next(reply["result"] for reply in replies if reply.get("id") == 3)
    assert check["structuredContent"]["error"]["category"] == "missing_key"
