"""Create a signed direct-install release from the Jaro Debian package."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

from package import APP_ID, ROOT, SYSTEM_PYTHON

sys.path.insert(0, str(ROOT / "engine"))
import linux_direct as direct


def build(package, output, signing_key, publisher):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".jaro-direct-", dir=output) as directory:
        work = Path(directory)
        extracted = work / "deb"
        subprocess.run(["dpkg-deb", "-x", package, extracted], check=True)
        payload = extracted / "usr/lib/jaro"
        metadata = payload / "portable-release.json"
        release = json.loads(metadata.read_bytes())
        if release["kind"] != "debian":
            raise ValueError("Supply the packaged Jaro Debian build.")
        version = release["version"]
        python = payload / "venv/bin/python"
        target_python = python.readlink()
        if not (target_python.parent == Path("/usr/bin") and target_python.name.startswith("python3.")):
            raise ValueError("The package uses an unexpected Python launcher.")
        for path in (python, payload / "in-use", payload / "bin/launch"):
            path.unlink()
        metadata.write_text(json.dumps({"kind": direct.KIND, "version": version, "revision": 0}), encoding="ascii")
        (payload / "share").mkdir(exist_ok=True)
        shutil.copyfile(extracted / f"usr/share/icons/hicolor/512x512/apps/{APP_ID}.png", payload / "share/jaro.png")
        shutil.copytree(extracted / "usr/share/doc/jaro", payload / "share/doc")
        archive = work / direct.ARCHIVE
        with tarfile.open(archive, mode="w:gz", compresslevel=3) as output_tar:
            for item in sorted(payload.iterdir()):
                output_tar.add(item, arcname=item.name)
        with tarfile.open(archive, "r|gz") as check:
            for _ in direct.archive_members(check):
                pass
        with archive.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        size = archive.stat().st_size
        key = signing_key.read_bytes()
        keyfile = work / "key"
        keyfile.touch(mode=0o600)
        # Native SDK keygen writes a raw Ed25519 seed. OpenSSL also accepts PEM.
        keyfile.write_bytes(bytes.fromhex("302e020100300506032b657004220420") + key if len(key) == 32 else key)
        message = work / "message"
        message.write_bytes(direct.signed_message(version, size, digest))
        signature = subprocess.check_output(["/usr/bin/openssl", "pkeyutl", "-sign", "-rawin",
                                             "-inkey", keyfile, "-in", message], timeout=10)
        manifest = work / direct.MANIFEST
        manifest.write_text(json.dumps({"version": version, "size": size, "sha256": digest,
                                       "signature": base64.b64encode(signature).decode("ascii")}) + "\n", encoding="ascii")
        direct.read_manifest(manifest, publisher)
        for asset in (archive, manifest):
            asset.replace(output / asset.name)
        shutil.copyfile(direct.__file__, output / "Jaro-linux-install.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--signing-key", type=Path, required=True)
    parser.add_argument("--publisher-key", default=direct.PUBLISHER)
    args = parser.parse_args()
    try:
        build(args.package, args.out, args.signing_key, args.publisher_key)
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
