from __future__ import annotations

import json
import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from quickprice.admin_account import (
    AdminAccountError,
    AdminAccountRevisionError,
    AdminAccountStore,
    create_admin_password_verifier,
    validate_admin_username,
    verify_admin_password,
)


def test_password_policy_accepts_any_eight_characters_and_bounds_length() -> None:
    verifier = create_admin_password_verifier("aaaaaaaa")
    assert verify_admin_password("aaaaaaaa", verifier) is True
    assert verify_admin_password("aaaaaaab", verifier) is False
    with pytest.raises(ValueError, match="8 to 256"):
        create_admin_password_verifier("1234567")
    with pytest.raises(ValueError, match="8 to 256"):
        create_admin_password_verifier("x" * 257)


@pytest.mark.parametrize("username", ("nova", "admin.user", "admin_user", "admin-user", "A1"))
def test_username_accepts_safe_ascii(username: str) -> None:
    assert validate_admin_username(username) == username


@pytest.mark.parametrize("username", ("", "a" * 65, "white space", "name@example", "name/slash"))
def test_username_rejects_unsafe_values(username: str) -> None:
    with pytest.raises(ValueError, match="username"):
        validate_admin_username(username)


def test_store_bootstraps_one_account_atomically_and_redacts_verifier(tmp_path) -> None:
    path = tmp_path / "admin-account.json"
    store = AdminAccountStore(path)
    verifier = create_admin_password_verifier("temporary-password")

    account = store.bootstrap(
        username="admin",
        password_verifier=verifier,
        password_change_required=True,
    )

    assert account.username == "admin"
    assert store.snapshot() == {
        "configured": True,
        "username": "admin",
        "password_change_required": True,
        "revision": account.revision,
    }
    assert verifier not in repr(store.snapshot())
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    persisted = AdminAccountStore(path).require_current()
    assert persisted == account

    other = store.bootstrap(
        username="other",
        password_verifier=create_admin_password_verifier("other-password"),
        password_change_required=False,
    )
    assert other == account


def test_store_replaces_account_with_revision_check(tmp_path) -> None:
    store = AdminAccountStore(tmp_path / "admin-account.json")
    first = store.bootstrap(
        username="admin",
        password_verifier=create_admin_password_verifier("temporary-password"),
    )
    replacement_verifier = create_admin_password_verifier("new-password")
    second = store.replace(
        username="nova",
        password_verifier=replacement_verifier,
        password_change_required=False,
        expected_revision=first.revision,
    )

    assert second.revision != first.revision
    assert second.username == "nova"
    assert second.password_change_required is False
    assert verify_admin_password("new-password", second.password_verifier)
    with pytest.raises(AdminAccountRevisionError):
        store.replace(
            username="admin",
            password_verifier=create_admin_password_verifier("another-password"),
            password_change_required=False,
            expected_revision=first.revision,
        )


def test_only_one_concurrent_revision_update_succeeds(tmp_path) -> None:
    store = AdminAccountStore(tmp_path / "admin-account.json")
    initial = store.bootstrap(
        username="admin",
        password_verifier=create_admin_password_verifier("temporary-password"),
    )
    verifier = create_admin_password_verifier("new-password")
    barrier = Barrier(2)

    def replace(username: str) -> str:
        barrier.wait()
        store.replace(
            username=username,
            password_verifier=verifier,
            password_change_required=False,
            expected_revision=initial.revision,
        )
        return username

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(replace, username) for username in ("nova", "admin2")]
        results = []
        for future in futures:
            try:
                results.append(("updated", future.result()))
            except AdminAccountRevisionError:
                results.append(("conflict", None))
    assert [outcome for outcome, _value in results].count("updated") == 1
    assert [outcome for outcome, _value in results].count("conflict") == 1


@pytest.mark.parametrize(
    "document",
    (
        {"version": 1},
        {
            "version": 2,
            "username": "admin",
            "password_verifier": "invalid",
            "password_change_required": True,
        },
        {
            "version": 1,
            "username": "admin",
            "password_verifier": "invalid",
            "password_change_required": True,
        },
        {
            "version": 1,
            "username": "admin",
            "password_verifier": "invalid",
            "password_change_required": True,
            "unexpected": True,
        },
    ),
)
def test_store_rejects_malformed_or_unknown_account_fields(tmp_path, document) -> None:
    path = tmp_path / "admin-account.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AdminAccountError):
        AdminAccountStore(path)


def test_store_rejects_symbolic_link_target(tmp_path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "admin-account.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(AdminAccountError, match="symbolic link"):
        AdminAccountStore(link)


def test_store_rejects_non_regular_account_path(tmp_path) -> None:
    path = tmp_path / "admin-account.json"
    path.mkdir()

    with pytest.raises(AdminAccountError, match="regular file"):
        AdminAccountStore(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are unavailable")
def test_store_rejects_group_or_world_accessible_account_file(tmp_path) -> None:
    path = tmp_path / "admin-account.json"
    store = AdminAccountStore(path)
    store.bootstrap(
        username="admin",
        password_verifier=create_admin_password_verifier("temporary-password"),
    )
    path.chmod(0o640)

    with pytest.raises(AdminAccountError, match="permissions must be 0600"):
        AdminAccountStore(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs are unavailable")
def test_store_rejects_inherited_windows_acl(tmp_path) -> None:
    path = tmp_path / "admin-account.json"
    store = AdminAccountStore(path)
    store.bootstrap(
        username="admin",
        password_verifier=create_admin_password_verifier("temporary-password"),
    )
    subprocess.run(
        ["icacls.exe", str(path), "/inheritance:e"],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    with pytest.raises(AdminAccountError, match="ACL must grant one owner only"):
        AdminAccountStore(path)
