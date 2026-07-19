"""Persistent single-account storage for administrator authentication."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Never

if os.name == "nt":
    from ctypes import wintypes

_ACCOUNT_VERSION: Final[int] = 1
_MAX_ACCOUNT_FILE_BYTES: Final[int] = 16 * 1024
_USERNAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_REVISION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_SCRYPT_NAME: Final[str] = "scrypt"
_SCRYPT_N: Final[int] = 2**15
_SCRYPT_R: Final[int] = 8
_SCRYPT_P: Final[int] = 1
_SCRYPT_MAXMEM: Final[int] = 128 * 1024 * 1024
_EMPTY_REVISION: Final[str] = hashlib.sha256(b"").hexdigest()
_ACCOUNT_FIELDS: Final[frozenset[str]] = frozenset(
    {"version", "username", "password_verifier", "password_change_required"}
)

_WINDOWS_FILE_ALL_ACCESS: Final[int] = 0x001F01FF
_WINDOWS_DACL_SECURITY_INFORMATION: Final[int] = 0x00000004
_WINDOWS_OWNER_SECURITY_INFORMATION: Final[int] = 0x00000001
_WINDOWS_PROTECTED_DACL_SECURITY_INFORMATION: Final[int] = 0x80000000
_WINDOWS_SE_DACL_PROTECTED: Final[int] = 0x1000


if os.name == "nt":

    class _WindowsSidAndAttributes(ctypes.Structure):
        _fields_ = (("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD))

    class _WindowsTokenUser(ctypes.Structure):
        _fields_ = (("user", _WindowsSidAndAttributes),)

    class _WindowsAcl(ctypes.Structure):
        _fields_ = (
            ("revision", wintypes.BYTE),
            ("reserved_1", wintypes.BYTE),
            ("size", wintypes.WORD),
            ("ace_count", wintypes.WORD),
            ("reserved_2", wintypes.WORD),
        )

    class _WindowsAceHeader(ctypes.Structure):
        _fields_ = (
            ("ace_type", wintypes.BYTE),
            ("ace_flags", wintypes.BYTE),
            ("ace_size", wintypes.WORD),
        )

    class _WindowsAccessAllowedAce(ctypes.Structure):
        _fields_ = (
            ("header", _WindowsAceHeader),
            ("mask", wintypes.DWORD),
            ("sid_start", wintypes.DWORD),
        )

    class _WindowsAclSizeInformation(ctypes.Structure):
        _fields_ = (
            ("ace_count", wintypes.DWORD),
            ("bytes_in_use", wintypes.DWORD),
            ("bytes_free", wintypes.DWORD),
        )


class AdminAccountError(RuntimeError):
    """Base error for persistent administrator account operations."""


class AdminAccountNotConfiguredError(AdminAccountError):
    """Raised when an operation requires an account but none exists."""


class AdminAccountRevisionError(AdminAccountError):
    """Raised when an account was changed concurrently."""


def _raise_windows_api_error(message: str, error_code: int | None = None) -> Never:
    code = ctypes.get_last_error() if error_code is None else error_code
    raise AdminAccountError(message) from ctypes.WinError(code)


def _windows_security_libraries() -> tuple[Any, Any]:
    if os.name != "nt":
        raise AdminAccountError("Windows account-file security is unavailable")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = (ctypes.c_void_p,)
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.CopySid.argtypes = (wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)
    advapi32.CopySid.restype = wintypes.BOOL
    advapi32.InitializeAcl.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD)
    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAceEx.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    )
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    advapi32.EqualSid.restype = wintypes.BOOL

    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    return advapi32, kernel32


def _current_windows_user_sid() -> Any:
    advapi32, kernel32 = _windows_security_libraries()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        _raise_windows_api_error("administrator account owner could not be resolved")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if not required.value:
            _raise_windows_api_error("administrator account owner could not be resolved")
        token_buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            1,
            token_buffer,
            required,
            ctypes.byref(required),
        ):
            _raise_windows_api_error("administrator account owner could not be resolved")
        sid_pointer = ctypes.cast(
            token_buffer,
            ctypes.POINTER(_WindowsTokenUser),
        ).contents.user.sid
        sid_length = advapi32.GetLengthSid(sid_pointer)
        if not sid_length:
            _raise_windows_api_error("administrator account owner SID is invalid")
        sid = ctypes.create_string_buffer(sid_length)
        if not advapi32.CopySid(sid_length, sid, sid_pointer):
            _raise_windows_api_error("administrator account owner SID could not be copied")
        return sid
    finally:
        kernel32.CloseHandle(token)


def _harden_windows_acl(path: Path) -> None:
    if os.name != "nt":
        return
    advapi32, _kernel32 = _windows_security_libraries()
    sid = _current_windows_user_sid()
    sid_length = advapi32.GetLengthSid(sid)
    acl_size = (
        ctypes.sizeof(_WindowsAcl)
        + ctypes.sizeof(_WindowsAccessAllowedAce)
        - ctypes.sizeof(wintypes.DWORD)
        + sid_length
    )
    acl = ctypes.create_string_buffer(acl_size)
    if not advapi32.InitializeAcl(acl, acl_size, 2):
        _raise_windows_api_error("administrator account ACL could not be initialized")
    if not advapi32.AddAccessAllowedAceEx(
        acl,
        2,
        0,
        _WINDOWS_FILE_ALL_ACCESS,
        sid,
    ):
        _raise_windows_api_error("administrator account ACL could not be created")
    result = advapi32.SetNamedSecurityInfoW(
        str(path),
        1,
        _WINDOWS_OWNER_SECURITY_INFORMATION
        | _WINDOWS_DACL_SECURITY_INFORMATION
        | _WINDOWS_PROTECTED_DACL_SECURITY_INFORMATION,
        sid,
        None,
        acl,
        None,
    )
    if result:
        _raise_windows_api_error("administrator account ACL could not be applied", result)


def _validate_windows_acl(path: Path) -> None:
    if os.name != "nt":
        return
    advapi32, kernel32 = _windows_security_libraries()
    current_sid = _current_windows_user_sid()
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        _WINDOWS_OWNER_SECURITY_INFORMATION | _WINDOWS_DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        _raise_windows_api_error("administrator account ACL could not be read", result)
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            _raise_windows_api_error("administrator account ACL control is invalid")
        information = _WindowsAclSizeInformation()
        if not dacl.value or not advapi32.GetAclInformation(
            dacl,
            ctypes.byref(information),
            ctypes.sizeof(information),
            2,
        ):
            _raise_windows_api_error("administrator account ACL is invalid")
        ace_pointer = ctypes.c_void_p()
        if information.ace_count != 1 or not advapi32.GetAce(
            dacl,
            0,
            ctypes.byref(ace_pointer),
        ):
            raise AdminAccountError("administrator account ACL must grant one owner only")
        ace = ctypes.cast(
            ace_pointer,
            ctypes.POINTER(_WindowsAccessAllowedAce),
        ).contents
        ace_sid = ctypes.c_void_p(ace_pointer.value + _WindowsAccessAllowedAce.sid_start.offset)
        if (
            not advapi32.EqualSid(owner, current_sid)
            or ace.header.ace_type != 0
            or not advapi32.EqualSid(ace_sid, current_sid)
            or ace.mask & _WINDOWS_FILE_ALL_ACCESS != _WINDOWS_FILE_ALL_ACCESS
            or not control.value & _WINDOWS_SE_DACL_PROTECTED
        ):
            raise AdminAccountError("administrator account ACL must grant one owner only")
    finally:
        if descriptor.value:
            kernel32.LocalFree(descriptor)


@dataclass(frozen=True, slots=True)
class AdminAccount:
    """Immutable administrator credential state.

    ``password_verifier`` is intentionally available only through this internal
    domain model. Public API responses should use :meth:`AdminAccountStore.snapshot`.
    """

    username: str
    password_verifier: str
    password_change_required: bool
    revision: str


def validate_admin_username(username: str) -> str:
    if not isinstance(username, str) or _USERNAME_PATTERN.fullmatch(username) is None:
        raise ValueError(
            "administrator username must contain 1 to 64 ASCII letters, digits, '.', '_', or '-'"
        )
    return username


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_admin_password_verifier(password: str) -> str:
    """Create a salted scrypt verifier for an 8-to-256-character password."""

    if not isinstance(password, str) or not 8 <= len(password) <= 256:
        raise ValueError("administrator password must contain 8 to 256 characters")
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        (
            _SCRYPT_NAME,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def parse_admin_password_verifier(value: str) -> tuple[int, int, int, bytes, bytes]:
    try:
        name, raw_n, raw_r, raw_p, raw_salt, raw_digest = value.split("$")
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        salt, digest = _b64decode(raw_salt), _b64decode(raw_digest)
    except (AttributeError, TypeError, ValueError, base64.binascii.Error) as exc:
        raise ValueError("invalid administrator password verifier") from exc
    if name != _SCRYPT_NAME or (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
        raise ValueError("unsupported administrator password verifier parameters")
    if len(salt) != 16 or len(digest) != 32:
        raise ValueError("invalid administrator password verifier length")
    return n, r, p, salt, digest


def verify_admin_password(password: str, verifier: str) -> bool:
    """Perform one complete scrypt verification and compare in constant time."""

    n, r, p, salt, expected = parse_admin_password_verifier(verifier)
    bounded = password if isinstance(password, str) and len(password) <= 256 else ""
    candidate = hashlib.scrypt(
        bounded.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=32,
        maxmem=_SCRYPT_MAXMEM,
    )
    return 8 <= len(bounded) <= 256 and hmac.compare_digest(candidate, expected)


def _revision(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _serialize_account(
    username: str,
    password_verifier: str,
    password_change_required: bool,
) -> bytes:
    document = {
        "version": _ACCOUNT_VERSION,
        "username": validate_admin_username(username),
        "password_verifier": password_verifier,
        "password_change_required": password_change_required,
    }
    parse_admin_password_verifier(password_verifier)
    if not isinstance(password_change_required, bool):
        raise ValueError("password_change_required must be a boolean")
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _parse_account(content: bytes) -> AdminAccount:
    if not content:
        raise AdminAccountNotConfiguredError("administrator account is not configured")
    if len(content) > _MAX_ACCOUNT_FILE_BYTES:
        raise AdminAccountError("administrator account file is too large")
    try:
        document: Any = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdminAccountError("administrator account file is invalid") from exc
    if not isinstance(document, dict) or set(document) != _ACCOUNT_FIELDS:
        raise AdminAccountError("administrator account file has an invalid schema")
    if document.get("version") != _ACCOUNT_VERSION:
        raise AdminAccountError("administrator account file version is unsupported")
    try:
        username = validate_admin_username(document.get("username"))
        password_verifier = document.get("password_verifier")
        parse_admin_password_verifier(password_verifier)
    except ValueError as exc:
        raise AdminAccountError("administrator account file is invalid") from exc
    password_change_required = document.get("password_change_required")
    if not isinstance(password_change_required, bool):
        raise AdminAccountError("administrator account file is invalid")
    return AdminAccount(
        username=username,
        password_verifier=password_verifier,
        password_change_required=password_change_required,
        revision=_revision(content),
    )


def _read_account_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return b""
    if stat.S_ISLNK(metadata.st_mode):
        raise AdminAccountError("administrator account path cannot be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise AdminAccountError("administrator account path must be a regular file")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise AdminAccountError("administrator account file permissions must be 0600")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise AdminAccountError("administrator account file owner is invalid")
    _validate_windows_acl(path)
    content = path.read_bytes()
    if len(content) > _MAX_ACCOUNT_FILE_BYTES:
        raise AdminAccountError("administrator account file is too large")
    return content


def _atomic_write(path: Path, content: bytes) -> None:
    if len(content) > _MAX_ACCOUNT_FILE_BYTES:
        raise AdminAccountError("administrator account file is too large")
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise AdminAccountError("administrator account directory cannot be a symbolic link")
    if path.is_symlink():
        raise AdminAccountError("administrator account path cannot be a symbolic link")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            _harden_windows_acl(temporary)
            _validate_windows_acl(temporary)
        else:
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _validate_windows_acl(path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = -1
        if directory_descriptor >= 0:
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


class AdminAccountStore:
    """Own one revisioned administrator account in an atomic local file."""

    empty_revision: Final[str] = _EMPTY_REVISION

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self._lock = threading.RLock()
        content = _read_account_file(self.path)
        self._account = _parse_account(content) if content else None

    @property
    def configured(self) -> bool:
        with self._lock:
            return self._account is not None

    def current(self) -> AdminAccount | None:
        """Return the current immutable internal credential record."""

        with self._lock:
            return self._account

    def require_current(self) -> AdminAccount:
        account = self.current()
        if account is None:
            raise AdminAccountNotConfiguredError("administrator account is not configured")
        return account

    def snapshot(self) -> dict[str, Any]:
        """Return account metadata without exposing its password verifier."""

        with self._lock:
            account = self._account
            return {
                "configured": account is not None,
                "username": account.username if account is not None else None,
                "password_change_required": (
                    account.password_change_required if account is not None else False
                ),
                "revision": account.revision if account is not None else _EMPTY_REVISION,
            }

    def bootstrap(
        self,
        *,
        username: str,
        password_verifier: str,
        password_change_required: bool = True,
    ) -> AdminAccount:
        """Create the account once, returning the existing account on later calls."""

        content = _serialize_account(username, password_verifier, password_change_required)
        with self._lock:
            current_content = _read_account_file(self.path)
            if current_content:
                self._account = _parse_account(current_content)
                return self._account
            _atomic_write(self.path, content)
            self._account = _parse_account(content)
            return self._account

    def replace(
        self,
        *,
        username: str,
        password_verifier: str,
        password_change_required: bool,
        expected_revision: str,
    ) -> AdminAccount:
        """Atomically replace the single account after an optimistic revision check."""

        if (
            not isinstance(expected_revision, str)
            or _REVISION_PATTERN.fullmatch(expected_revision) is None
        ):
            raise ValueError("expected_revision must be a SHA-256 hexadecimal digest")
        content = _serialize_account(username, password_verifier, password_change_required)
        with self._lock:
            current_content = _read_account_file(self.path)
            current_revision = _revision(current_content)
            if not hmac.compare_digest(current_revision, expected_revision):
                raise AdminAccountRevisionError("administrator account changed concurrently")
            if not current_content:
                raise AdminAccountNotConfiguredError("administrator account is not configured")
            _parse_account(current_content)
            _atomic_write(self.path, content)
            self._account = _parse_account(content)
            return self._account


__all__ = [
    "AdminAccount",
    "AdminAccountError",
    "AdminAccountNotConfiguredError",
    "AdminAccountRevisionError",
    "AdminAccountStore",
    "create_admin_password_verifier",
    "parse_admin_password_verifier",
    "validate_admin_username",
    "verify_admin_password",
]
