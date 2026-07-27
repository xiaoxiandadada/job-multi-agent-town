import importlib.util
from types import SimpleNamespace
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "verify_feishu_credential.py"
)
SPEC = importlib.util.spec_from_file_location(
    "verify_feishu_credential",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
verify_credential = MODULE.verify_credential


def test_verify_credential_accepts_valid_tenant_token(monkeypatch):
    captured = {}

    def fake_post(url, *, json, timeout):
        captured.update(url=url, payload=json, timeout=timeout)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "code": 0,
                "tenant_access_token": "tenant-token",
            },
        )

    monkeypatch.setattr(
        MODULE.httpx,
        "post",
        fake_post,
    )

    verified, message = verify_credential("cli_test", "secret-value")

    assert verified is True
    assert message == "credential_verified"
    assert captured["payload"] == {
        "app_id": "cli_test",
        "app_secret": "secret-value",
    }
    assert "secret-value" not in message


def test_verify_credential_rejects_invalid_pair_without_echoing_secret(
    monkeypatch,
):
    monkeypatch.setattr(
        MODULE.httpx,
        "post",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            json=lambda: {"code": 10003, "msg": "invalid app credential"},
        ),
    )

    verified, message = verify_credential("cli_test", "sensitive-secret")

    assert verified is False
    assert "code=10003" in message
    assert "sensitive-secret" not in message
