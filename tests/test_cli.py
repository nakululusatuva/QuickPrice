from __future__ import annotations

import sys

import pytest

from quickprice import __main__
from quickprice.admin_account import verify_admin_password
from quickprice.config import Settings


def test_serve_disables_uvicorn_implicit_proxy_header_trust(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def run(application: str, **kwargs) -> None:
        captured["application"] = application
        captured.update(kwargs)

    monkeypatch.setattr(sys, "argv", ["quickprice", "serve"])
    monkeypatch.setattr(
        __main__.Settings,
        "from_env",
        classmethod(
            lambda _cls: Settings(
                production=False,
                require_free_threaded=False,
                background_enabled=False,
            )
        ),
    )
    monkeypatch.setattr(__main__.uvicorn, "run", run)

    __main__.main()

    assert captured["application"] == "quickprice.api:app"
    assert captured["proxy_headers"] is False
    assert captured["workers"] == 1
    assert captured["http"] == "h11"


def test_admin_credentials_generate_password_bootstrap_without_plaintext_argv(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "quickprice",
            "admin-credentials",
            "--origin",
            "https://quickprice.example.com/",
        ],
    )

    __main__.main()

    output = capsys.readouterr().out.splitlines()
    password = next(
        line.removeprefix("Temporary administrator password (save it now): ")
        for line in output
        if line.startswith("Temporary administrator password")
    )
    verifier = next(
        line.removeprefix("QUICKPRICE_ADMIN_PASSWORD_VERIFIER=")
        for line in output
        if line.startswith("QUICKPRICE_ADMIN_PASSWORD_VERIFIER=")
    )
    assert "Administrator username: admin" in output
    assert "QUICKPRICE_ADMIN_USERNAME=admin" in output
    assert "QUICKPRICE_ADMIN_PASSWORD_CHANGE_REQUIRED=true" in output
    assert "QUICKPRICE_ADMIN_ORIGIN=https://quickprice.example.com" in output
    assert any(line.startswith("TOTP URI: otpauth://totp/QuickPrice%3Aadmin?") for line in output)
    assert verify_admin_password(password, verifier)
    assert not any("ADMIN_KEY_VERIFIER" in line for line in output)
    assert not any(line.startswith("QUICKPRICE_ADMIN_PASSWORD=") for line in output)


def test_admin_credentials_accept_an_explicit_valid_username(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "quickprice",
            "admin-credentials",
            "--origin",
            "http://localhost:8080",
            "--username",
            "ops_admin",
        ],
    )

    __main__.main()

    assert "QUICKPRICE_ADMIN_USERNAME=ops_admin" in capsys.readouterr().out


def test_admin_credentials_reject_plaintext_password_argv(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "quickprice",
            "admin-credentials",
            "--origin",
            "https://quickprice.example.com",
            "--password",
            "must-not-enter-process-argv",
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        __main__.main()


def test_admin_password_verifier_preserves_existing_totp_and_reads_password_securely(
    monkeypatch, capsys
) -> None:
    prompts: list[str] = []

    def read_password(prompt: str) -> str:
        prompts.append(prompt)
        return "new-password"

    monkeypatch.setattr(
        sys,
        "argv",
        ["quickprice", "admin-password-verifier", "--username", "nova"],
    )
    monkeypatch.setattr(__main__.getpass, "getpass", read_password)

    __main__.main()

    output = capsys.readouterr().out.splitlines()
    verifier = next(
        line.removeprefix("QUICKPRICE_ADMIN_PASSWORD_VERIFIER=")
        for line in output
        if line.startswith("QUICKPRICE_ADMIN_PASSWORD_VERIFIER=")
    )
    assert prompts == ["Administrator password: ", "Confirm administrator password: "]
    assert "QUICKPRICE_ADMIN_USERNAME=nova" in output
    assert "QUICKPRICE_ADMIN_PASSWORD_CHANGE_REQUIRED=true" in output
    assert verify_admin_password("new-password", verifier)
    assert not any("TOTP" in line for line in output)
