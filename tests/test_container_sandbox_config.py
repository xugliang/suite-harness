import pytest
from pydantic import ValidationError

from suiteharness.config.models import DockerSandboxConfig


def test_existing_docker_config_uses_direct_backend():
    assert DockerSandboxConfig(image="sandbox:1").broker_socket is None


def test_broker_socket_accepts_absolute_path(tmp_path):
    socket = tmp_path / "sandbox.sock"
    assert DockerSandboxConfig(image="sandbox:1", broker_socket=socket).broker_socket == socket


@pytest.mark.parametrize("socket", ["relative.sock", "../sandbox.sock", "bad\x00socket"])
def test_broker_socket_rejects_invalid_path(socket):
    with pytest.raises(ValidationError):
        DockerSandboxConfig(image="sandbox:1", broker_socket=socket)


def test_broker_does_not_relax_required_sandbox(tmp_path):
    with pytest.raises(ValidationError):
        DockerSandboxConfig(image="sandbox:1", broker_socket=tmp_path / "broker", required=False)
