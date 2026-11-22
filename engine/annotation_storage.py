"""Identity and recovery for annotation media outside the Session library."""
import errno
import os
import shutil
import stat
from pathlib import Path
from uuid import UUID, uuid4

from meeting_retention import sync_directory

SCHEMA = """
CREATE TABLE IF NOT EXISTS annotation_locations (
    id TEXT PRIMARY KEY, folder TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS annotation_location_preference (
    id INTEGER PRIMARY KEY CHECK(id=1),
    location TEXT REFERENCES annotation_locations(id)
);
CREATE TABLE IF NOT EXISTS annotation_file_operations (
    id TEXT PRIMARY KEY,
    location TEXT NOT NULL REFERENCES annotation_locations(id),
    operation TEXT NOT NULL CHECK(operation IN ('import','remove'))
);
"""
ROOT_MARKER = ".jaro-annotation-folder"
CAPTURE_MARKER = ".jaro-annotation-id"


class Unavailable(OSError):
    def __init__(self):
        super().__init__("The Annotations folder is unavailable. Reconnect it or choose another folder for new captures.")


def canonical_id(value):
    try:
        return isinstance(value, str) and len(value) == 36 and str(UUID(value)) == value
    except ValueError:
        return False


def read_marker(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Unavailable()
        try:
            key = file.read(37).decode("ascii")
            if canonical_id(key):
                return key
        except ValueError:
            pass
        raise Unavailable()


def write_marker(path, key):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(key.encode("ascii"))
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def root_path(folder, location):
    try:
        path = Path(folder)
        if path.resolve(strict=True) == path and path.is_dir() and read_marker(path / ROOT_MARKER) == location:
            return path
    except (OSError, ValueError, RuntimeError):
        pass
    raise Unavailable()


def location_root(db, location):
    row = db.execute("SELECT folder FROM annotation_locations WHERE id=?", (location,)).fetchone()
    if row is None:
        raise Unavailable()
    return root_path(row[0], location)


def current(db):
    return db.execute("""SELECT a.id,a.folder FROM annotation_location_preference p
                         JOIN annotation_locations a ON a.id=p.location WHERE p.id=1""").fetchone()


def status(db, default):
    row = current(db)
    if row is None:
        return {"folder": str(default), "storageId": "default", "custom": False, "available": True}
    try:
        root_path(row["folder"], row["id"])
        available = True
    except Unavailable:
        available = False
    return {"folder": row["folder"], "storageId": row["id"], "custom": True, "available": available}


def select(db, folder, library_root):
    location = None
    if folder is not None:
        try:
            path = Path(folder)
            if not path.is_absolute() or not 0 < len(folder) <= 4096:
                raise ValueError
            path = path.resolve(strict=True)
            library = library_root.resolve(strict=True)
            if not path.is_dir() or path.is_relative_to(library) or library.is_relative_to(path):
                raise ValueError
        except (OSError, ValueError, RuntimeError):
            raise ValueError("Choose an existing folder outside the Jaro library.") from None
        marker = path / ROOT_MARKER
        try:
            location = read_marker(marker)
        except FileNotFoundError:
            location = str(uuid4())
            try:
                write_marker(marker, location)
                sync_directory(path)
            except FileExistsError:
                location = read_marker(marker)
        existing = db.execute("SELECT folder FROM annotation_locations WHERE id=?", (location,)).fetchone()
        if existing and existing[0] != str(path):
            raise ValueError("This annotation folder is registered at another location.")
        db.execute("INSERT OR IGNORE INTO annotation_locations(id,folder) VALUES(?,?)", (location, str(path)))
    db.execute("INSERT INTO annotation_location_preference VALUES(1,?) ON CONFLICT(id) DO UPDATE SET location=excluded.location",
               (location,))


def bundle_path(root, key):
    if not canonical_id(key):
        raise ValueError("The annotation ID is invalid.")
    return root / f"Jaro Annotation {key}"


def check_bundle(folder, key):
    try:
        if not folder.is_symlink() and folder.is_dir() and read_marker(folder / CAPTURE_MARKER) == key:
            return folder
    except (OSError, ValueError):
        pass
    raise Unavailable()


def folder_for(db, default, key, location):
    if location is None:
        return default / key
    return check_bundle(bundle_path(location_root(db, location), key), key)


def allocate(root, key):
    folder = bundle_path(root, key)
    folder.mkdir(mode=0o700)
    try:
        write_marker(folder / CAPTURE_MARKER, key)
        sync_directory(folder)
        sync_directory(root)
    except BaseException:
        shutil.rmtree(folder)
        raise
    return folder


def duplicate(source, destination):
    try:
        os.link(source, destination, follow_symlinks=False)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        with source.open("rb") as reader, destination.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            shutil.copyfileobj(reader, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())


def recover(db, *, key=None):
    rows = db.execute("SELECT * FROM annotation_file_operations WHERE ? IS NULL OR id=?", (key, key)).fetchall()
    for row in rows:
        identifier = row["id"]
        if db.execute("SELECT 1 FROM sessions WHERE id=?", (identifier,)).fetchone():
            db.execute("DELETE FROM annotation_file_operations WHERE id=?", (identifier,))
            continue
        try:
            root = location_root(db, row["location"])
            folder = bundle_path(root, identifier)
            if os.path.lexists(folder):
                check_bundle(folder, identifier)
                shutil.rmtree(folder)
                sync_directory(root)
        except OSError:
            # The drive may be disconnected. Keep the operation for another pass.
            continue
        db.execute("DELETE FROM annotation_file_operations WHERE id=?", (identifier,))
