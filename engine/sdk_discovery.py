"""Owner-scoped advertisement for the local transcription SDK."""
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

from engine_lifecycle import engine_proof, library_id

SDK_VERSION = 1
SDK_DISCOVERY = ".jard-sdk.json"
SDK_MAX_UPLOAD = 64 * 1024 * 1024
SDK_MAX_REQUESTS = 2
REGISTRY = r"Software\Jaro\SDK"


def sdk_library_id(root):
    folder = root.stat()
    return hashlib.sha256(f"{folder.st_dev}:{folder.st_ino}".encode("ascii")).hexdigest()


def sdk_identity(root, port):
    return {
        "apiVersion": SDK_VERSION, "pid": os.getpid(), "port": port,
        "libraryId": sdk_library_id(root), "token": secrets.token_hex(32),
    }


def registry_key(root):
    return library_id(os.path.normcase(root))


def read_sdk_record(root, port=None):
    try:
        if os.name == "nt":
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY, access=winreg.KEY_QUERY_VALUE) as registry:
                data, kind = winreg.QueryValueEx(registry, registry_key(root))
            if kind != winreg.REG_SZ:
                raise ValueError("invalid discovery record type")
        else:
            descriptor = os.open(root / SDK_DISCOVERY, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                details = os.fstat(source.fileno())
                if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o077:
                    raise ValueError("invalid discovery file")
                data = source.read(4097)
        if len(data) > 4096:
            raise ValueError("oversized discovery file")
        record = json.loads(data)
        if (type(record["apiVersion"]) is not int or record["apiVersion"] != SDK_VERSION
                or type(record["pid"]) is not int or record["pid"] <= 1
                or type(record["port"]) is not int or not 1 <= record["port"] <= 65535
                or (port is not None and record["port"] != port) or record["libraryId"] != sdk_library_id(root)
                or not re.fullmatch(r"[0-9a-f]{64}", record["token"])):
            raise ValueError("invalid discovery record")
        return record
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RuntimeError("Could not verify the running Jaro transcription service.") from error


def publish_sdk(root, port, *, token=None):
    record = sdk_identity(root, port)
    if token is not None:
        record["token"] = token
    if os.name == "nt":
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY, access=winreg.KEY_SET_VALUE) as registry:
            winreg.SetValueEx(registry, registry_key(root), 0, winreg.REG_SZ, json.dumps(record))
        return record
    descriptor, temporary = tempfile.mkstemp(prefix=".sdk-", dir=root)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(record, output)
            output.write("\n")
        os.replace(temporary, root / SDK_DISCOVERY)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return record


def remove_sdk(root, record):
    try:
        if read_sdk_record(root, record["port"]) != record:
            return
        if os.name == "nt":
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY, access=winreg.KEY_SET_VALUE) as registry:
                winreg.DeleteValue(registry, registry_key(root))
        else:
            (root / SDK_DISCOVERY).unlink(missing_ok=True)
    except (OSError, RuntimeError):
        pass


def sdk_proof(record, challenge):
    identity = ":".join(str(record[key]) for key in ("apiVersion", "pid", "port", "libraryId"))
    return engine_proof(record["token"], f"{identity}:{challenge}")


if __name__ == "__main__":
    # The packaged Windows Node client reads the current user's private record
    # through its own child process, never through a shell or an HTTP response.
    if os.name != "nt" or len(sys.argv) != 2:
        sys.exit(1)
    try:
        print(json.dumps(read_sdk_record(Path(sys.argv[1]).expanduser().resolve())))
    except RuntimeError as error:
        sys.exit(2 if isinstance(error.__cause__, FileNotFoundError) else 1)
