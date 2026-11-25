"""Verify Windows files and local TCP listeners used by agent handoffs."""

from contextlib import contextmanager
import ctypes as c
from ctypes import wintypes as w
from ipaddress import ip_address
import msvcrt
import os
import socket
import stat
from struct import Struct


def bind(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = arguments
    return function


kernel = c.WinDLL("kernel32", use_last_error=True)
security = c.WinDLL("advapi32", use_last_error=True)
ip_helper = c.WinDLL("iphlpapi")
pointer = c.c_void_p
pp = c.POINTER(pointer)
pdword = c.POINTER(w.DWORD)

close_handle = bind(kernel, "CloseHandle", w.BOOL, w.HANDLE)
current_process = bind(kernel, "GetCurrentProcess", w.HANDLE)
open_process = bind(kernel, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
open_token = bind(security, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, c.POINTER(w.HANDLE))
token_information = bind(security, "GetTokenInformation", w.BOOL, w.HANDLE, w.DWORD, pointer, w.DWORD, pdword)
equal_sid = bind(security, "EqualSid", w.BOOL, pointer, pointer)
well_known_sid = bind(security, "IsWellKnownSid", w.BOOL, pointer, c.c_int)
file_security = bind(security, "GetSecurityInfo", w.DWORD, w.HANDLE, c.c_int, w.DWORD, pp, pp, pp, pp, pp)
free_local = bind(kernel, "LocalFree", pointer, pointer)
sid_text = bind(security, "ConvertSidToStringSidW", w.BOOL, pointer, pp)
text_security = bind(security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL,
                     w.LPCWSTR, w.DWORD, pp, pdword)
open_file = bind(kernel, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, pointer, w.DWORD, w.DWORD, w.HANDLE)
file_type = bind(kernel, "GetFileType", w.DWORD, w.HANDLE)
read_ace = bind(security, "GetAce", w.BOOL, pointer, w.DWORD, pp)
tcp_table = bind(ip_helper, "GetExtendedTcpTable", w.DWORD, pointer, pdword, w.BOOL, w.DWORD, c.c_int, w.DWORD)

# Local System, built-in Administrators, and the verified file owner's rights.
# CPython uses Owner Rights for directories created with mode 0o700.
PRIVATE_SID_TYPES = (22, 26, 71)


class SecurityAttributes(c.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", pointer), ("inheritable", w.BOOL)]


@contextmanager
def token_user(process):
    token = w.HANDLE()
    if not open_token(process, 0x0008, c.byref(token)):  # TOKEN_QUERY
        raise c.WinError(c.get_last_error())
    try:
        size = w.DWORD()
        token_information(token, 1, None, 0, c.byref(size))  # TokenUser
        if not size.value:
            raise c.WinError(c.get_last_error())
        data = c.create_string_buffer(size.value)
        if not token_information(token, 1, data, size, c.byref(size)):
            raise c.WinError(c.get_last_error())
        yield pointer.from_buffer(data).value  # TOKEN_USER starts with its SID pointer.
    finally:
        close_handle(token)


@contextmanager
def private_creation_attributes():
    name = pointer()
    with token_user(current_process()) as user:
        if not sid_text(user, c.byref(name)):
            raise c.WinError(c.get_last_error())
    try:
        owner = c.wstring_at(name)
    finally:
        free_local(name)
    descriptor = pointer()
    policy = f"O:{owner}D:P(A;;FA;;;{owner})(A;;FA;;;SY)(A;;FA;;;BA)"
    if not text_security(policy, 1, c.byref(descriptor), None):
        raise c.WinError(c.get_last_error())
    try:
        yield SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
    finally:
        free_local(descriptor)


def private_acl(handle):
    owner, acl, descriptor = pointer(), pointer(), pointer()
    # SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    if file_security(handle, 1, 5, c.byref(owner), None, c.byref(acl), None, c.byref(descriptor)):
        return False
    try:
        with token_user(current_process()) as user:
            if not (owner and acl and equal_sid(owner, user)):
                return False
            # The ACL header's AceCount is a WORD at byte four.
            for index in range(c.c_ushort.from_address(acl.value + 4).value):
                ace = pointer()
                if not read_ace(acl, index, c.byref(ace)):
                    return False
                kind, flags = (c.c_ubyte * 2).from_address(ace.value)
                if flags & 0x08 or kind in (1, 6, 10, 12):  # Inherit-only and access-denied ACEs.
                    continue
                if kind != 0:  # Plain ACCESS_ALLOWED_ACE
                    return False
                sid = ace.value + 8  # SidStart follows the header and access mask.
                if not equal_sid(sid, user) and not any(well_known_sid(sid, kind) for kind in PRIVATE_SID_TYPES):
                    return False
            return True
    finally:
        free_local(descriptor)


def open_private_file(path, *, write=False):
    path = path.absolute()
    if len(path.drive) != 2 or not path.drive.endswith(":"):
        raise ValueError("Agent handoff files require a local Windows drive.")
    # OPEN_REPARSE_POINT opens the leaf itself. OPEN_ALWAYS only applies the
    # supplied ACL when creating a file and leaves existing files untouched.
    invalid_handle = pointer(-1).value
    if write:
        with private_creation_attributes() as attributes:
            handle = open_file(str(path), 0xC0000000, 7, c.byref(attributes), 4, 0x00200000, None)
            if handle == invalid_handle:
                raise c.WinError(c.get_last_error())
    else:
        handle = open_file(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
        if handle == invalid_handle:
            raise c.WinError(c.get_last_error())
    try:
        if file_type(handle) != 1 or not private_acl(handle):
            raise ValueError("Agent handoff files must be private to the current Windows user.")
        fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY))
    except BaseException:
        close_handle(handle)
        raise
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode) or details.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Agent handoff files must be regular files.")
    except BaseException:
        os.close(fd)
        raise
    return fd


TCP4_ROW = Struct("<I4s2s10xI")
TCP6_ROW = Struct("<16s4x2s26xII")


def listener_pids(address, port):
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    layout = TCP6_ROW if address.version == 6 else TCP4_ROW
    size = w.DWORD(0)
    data = None
    for _ in range(3):
        result = tcp_table(data, c.byref(size), False, family, 3, 0)  # TCP_TABLE_OWNER_PID_LISTENER
        if result == 0 and data is not None:
            count = w.DWORD.from_buffer(data).value
            end = 4 + count * layout.size
            if end > len(data):
                return set()
            bound = {address.packed, bytes(len(address.packed))}
            pids = set()
            for row in layout.iter_unpack(memoryview(data)[4:end]):
                local, number, state, pid = row if address.version == 6 else (row[1], row[2], row[0], row[3])
                if state == 2 and local in bound and int.from_bytes(number, "big") == port:
                    pids.add(pid)
            return pids
        if result != 122 or not 4 <= size.value <= 4 * 1024 * 1024:  # ERROR_INSUFFICIENT_BUFFER
            break
        data = c.create_string_buffer(size.value)
    return set()


def owns_listener(pid, host, port):
    process = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return False
    try:
        with token_user(process) as owner, token_user(current_process()) as user:
            return bool(equal_sid(owner, user)) and listener_pids(ip_address(host.strip("[]")), port) == {pid}
    except OSError:
        return False
    finally:
        close_handle(process)
