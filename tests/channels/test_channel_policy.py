from __future__ import annotations

from suiteharness.channels import CompanyChannelAuthorizationPolicy, WorkspaceWriteRule
from suiteharness.execution import RunRequest, ToolAuthorization, ToolEffect, ToolIdentity, ToolSpec
from suiteharness.runtime import RequestScope, ScopePath


def _request(channel: str) -> RunRequest:
    return RunRequest(
        run_id="run-1",
        grant_id="grant-1",
        input="test",
        scope=RequestScope(
            ScopePath.agent("acme", "sales", "default", "session-1"),
            "user-1",
            channel_id=channel,
        ),
    )


def _identity(name: str, origin: str = "suiteharness.builtin.fs") -> ToolIdentity:
    return ToolIdentity(namespace="suiteharness", name=name, origin=origin, version="1")


def _spec(name: str, *effects: ToolEffect) -> ToolSpec:
    return ToolSpec(name=name, effects=frozenset(effects))


def test_web_reads_are_direct_and_writes_require_exact_approval() -> None:
    policy = CompanyChannelAuthorizationPolicy()

    assert policy.evaluate(
        _request("web"),
        _identity("suiteharness.web.search", "suiteharness.builtin.web"),
        _spec("suiteharness.web.search", ToolEffect.READ, ToolEffect.EXTERNAL),
        {"query": "SuiteHarness"},
    ) is ToolAuthorization.ALLOW
    assert policy.evaluate(
        _request("web"),
        _identity("suiteharness.fs.write"),
        _spec("suiteharness.fs.write", ToolEffect.WRITE),
        {"path": "report.md"},
    ) is ToolAuthorization.REQUIRE_APPROVAL


def test_feishu_only_allows_reads_and_configured_builtin_file_writes() -> None:
    policy = CompanyChannelAuthorizationPolicy(
        feishu_writable_roots=(WorkspaceWriteRule("product", "exports"),)
    )
    request = _request("feishu")

    assert policy.evaluate(
        request,
        _identity("suiteharness.fs.read"),
        _spec("suiteharness.fs.read", ToolEffect.READ),
        {"path": "private/input.txt"},
    ) is ToolAuthorization.ALLOW
    assert policy.evaluate(
        request,
        _identity("suiteharness.fs.write"),
        _spec("suiteharness.fs.write", ToolEffect.WRITE),
        {"path": "exports/report.md"},
    ) is ToolAuthorization.ALLOW
    assert policy.evaluate(
        request,
        _identity("suiteharness.fs.write"),
        _spec("suiteharness.fs.write", ToolEffect.WRITE),
        {"path": "private/report.md"},
    ) is ToolAuthorization.DENY
    assert policy.evaluate(
        request,
        _identity("suiteharness.shell.bash", "suiteharness.builtin.shell"),
        _spec("suiteharness.shell.bash", ToolEffect.WRITE),
        {"command": "touch exports/a"},
    ) is ToolAuthorization.DENY


def test_feishu_can_never_bypass_destructive_denial() -> None:
    policy = CompanyChannelAuthorizationPolicy(
        feishu_writable_roots=(WorkspaceWriteRule("product", "."),)
    )
    assert policy.evaluate(
        _request("feishu"),
        _identity("suiteharness.fs.delete"),
        _spec("suiteharness.fs.delete", ToolEffect.WRITE, ToolEffect.DESTRUCTIVE),
        {"path": "x"},
    ) is ToolAuthorization.DENY


def test_untrusted_path_spelling_never_matches_a_feishu_root() -> None:
    rule = WorkspaceWriteRule("product", "exports")

    assert not rule.permits("product", "exports/../private.txt")
    assert not rule.permits("product", "C:\\exports\\private.txt")
    assert not rule.permits("shared", "exports/a.txt")
