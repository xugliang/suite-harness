import asyncio
import json
import logging
from dataclasses import replace

import pytest

from suiteharness import diagnostics as diag
from suiteharness.execution import RunStatus
from suiteharness.execution.protocols import ExecutionStrategies
from suiteharness.agents.react import ReActWorkflow
from suiteharness.models.errors import ModelErrorCode, ModelProviderError
import importlib.util
from pathlib import Path

def helper(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

components = helper("test_execution_stream").components
PausedGateway = helper("test_execution_stream").PausedGateway
build = helper("test_model_diagnostics").build


@pytest.fixture
def records():
    result = []
    class Capture(logging.Handler):
        def emit(self, record):
            result.append(json.loads(record.getMessage()))
    handler = Capture()
    old = diag._logger.level
    diag._logger.setLevel(logging.INFO)
    diag._logger.addHandler(handler)
    yield result
    diag._logger.removeHandler(handler)
    diag._logger.setLevel(old)


def test_safe_exception_chain_and_unknown_fields_are_not_logged(records):
    try:
        try:
            raise ValueError("SECRET_TOKEN customer_name exam_answer")
        except ValueError as exc:
            raise ModelProviderError(ModelErrorCode.INVALID_REQUEST, "SECRET_BODY", status_code=400) from exc
    except Exception as exc:
        with diag.bind(request_id="request-" + "a" * 40):
            diag.emit("failure", error=exc, prompt="SECRET_PROMPT", headers="SECRET_HEADERS")
    text = json.dumps(records)
    assert "SECRET" not in text and "customer_name" not in text
    assert [e["type"] for e in records[-1]["exceptions"]] == ["ModelProviderError", "ValueError"]
    assert records[-1]["exceptions"][0]["code"] == "invalid_request"


def test_concurrent_run_context_and_thread_do_not_leak(records):
    async def check(name):
        runner, request = await components()
        request = request.model_copy(update={"run_id": name, "scope": replace(request.scope, request_id="request-" + name * 40)})
        gateway = PausedGateway(truncate=True)
        gateway.release.set()
        result = await runner.run(request, ExecutionStrategies(workflow=ReActWorkflow(gateway, "chat")))
        assert result.status is RunStatus.FAILED
        assert diag._context.get() == {}
    async def run():
        await asyncio.gather(check("a"), check("b"))
        with diag.bind(request_id="request-" + "c" * 40):
            await asyncio.to_thread(diag.emit, "thread.event")
        assert diag._context.get() == {}
    asyncio.run(run())
    failures = [r for r in records if r["event"] == "workflow.failed"]
    assert len(failures) == 2
    for r in failures:
        assert r["request_id"] == "request-" + r["run_id"] * 40
        assert r["exceptions"][0]["type"] == "ReActStateError"
        assert any(f["file"] == "agents/react.py" for f in r["exceptions"][0]["frames"])
    assert next(r for r in records if r["event"] == "thread.event")["request_id"] == "request-" + "c" * 40


@pytest.mark.parametrize("after_text", [False, True])
def test_provider_failure_before_or_after_first_event(records, after_text):
    gateway, provider, request = build()
    from suiteharness.models import ModelStreamEvent, ModelStreamEventType
    async def broken(request):
        if after_text:
            yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta="SECRET_PARTIAL")
        raise ModelProviderError(ModelErrorCode.TIMEOUT, "SECRET_HTTP_BODY", retryable=True)
    provider.stream = broken
    async def run():
        with pytest.raises(ModelProviderError):
            async for _ in gateway.stream("test", request):
                pass
    asyncio.run(run())
    failures = [r for r in records if r["event"] == "model.failed"]
    assert failures[-1]["emitted"] is after_text
    assert failures[-1]["exceptions"][0]["code"] == "timeout"
    assert "SECRET" not in json.dumps(records)
    ids = {r["model_call_id"] for r in records if "model_call_id" in r}
    assert len(ids) == 1


def test_sink_failure_does_not_escape(records, capsys):
    class Broken(logging.Handler):
        def emit(self, record):
            raise OSError("SECRET_DISK_PATH")
    handler = Broken()
    diag._logger.addHandler(handler)
    try:
        diag.emit("safe.event")
    finally:
        diag._logger.removeHandler(handler)
    assert "diagnostics output unavailable" in capsys.readouterr().err


def test_context_capacity_failure_is_distinct(records):
    from suiteharness.models.context import ContextCapacityError
    gateway, _, request = build()
    def fail(*args):
        raise ContextCapacityError("SECRET_CONTEXT")
    gateway._prepare_context = fail
    async def run():
        with pytest.raises(ContextCapacityError):
            async for _ in gateway.stream("test", request):
                pass
    asyncio.run(run())
    event = next(r for r in records if r["event"] == "model.context_failed")
    assert event["stage"] == "context_prepare"
    assert event["exceptions"][0]["type"] == "ContextCapacityError"
    assert not any(r["event"] == "model.attempt_started" for r in records)
    assert "SECRET" not in json.dumps(records)


def test_cancelled_stream_logs_cancelled_without_changing_result(records):
    gateway, _, request = build()
    async def run():
        stream = gateway.stream("test", request)
        await anext(stream)
        await stream.aclose()
    asyncio.run(run())
    assert next(r for r in records if r["event"] == "model.finished")["status"] == "cancelled"


def test_rotation_and_single_request_export(tmp_path, capsys):
    old_handlers = list(diag._logger.handlers)
    for h in old_handlers:
        diag._logger.removeHandler(h)
    try:
        diag.configure(tmp_path / "diagnostics.jsonl", max_bytes=1024, backups=2)
        for _ in range(24):
            diag.emit("unit.event", request_id="request-" + "a" * 40)
        diag.emit("unit.other", request_id="request-" + "b" * 40)
        assert len(list(tmp_path.glob("diagnostics.jsonl*"))) <= 3
        capsys.readouterr()
        diag.main(["--request-id", "request-" + "a" * 40, "--logs", str(tmp_path)])
        lines = capsys.readouterr().out.splitlines()
        assert lines and all(json.loads(line)["request_id"] == "request-" + "a" * 40 for line in lines)
    finally:
        for h in list(diag._logger.handlers):
            diag._logger.removeHandler(h)
            h.close()
        for h in old_handlers:
            diag._logger.addHandler(h)


def test_export_ignores_corrupt_records_and_unknown_content(tmp_path, capsys):
    request_id = "request-" + "c" * 40
    record = {"schema": "suiteharness.diagnostic.v1", "request_id": request_id,
              "event": "unit.event", "prompt": "SECRET", "exceptions": None}
    records = ["not-json", "[]", json.dumps(record)]
    record["exceptions"] = [{"type": "ValueError", "frames": None, "message": "SECRET"}]
    records.append(json.dumps(record))
    (tmp_path / "diagnostics.jsonl").write_text("\n".join(records), encoding="utf-8")
    diag.main(["--request-id", request_id, "--logs", str(tmp_path)])
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert len(output.splitlines()) == 2
