"""Private image attachments for saved sessions."""
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid4

import meeting_retention

MAX_BYTES = 512 * 1024
MAX_IMAGES = 16
SCHEMA = """
CREATE TABLE IF NOT EXISTS session_images (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    image BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS session_images_owner ON session_images(session_id);
"""


def jpeg_dimensions(image):
    """Read the dimensions of a baseline or progressive JPEG frame."""
    if not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
        return None
    position = 2
    while position + 4 <= len(image) and image[position] == 0xff:
        marker = image[position + 1]
        size = int.from_bytes(image[position + 2:position + 4], "big")
        if size < 2 or position + 2 + size > len(image):
            return None
        if marker in (0xc0, 0xc2):
            if size < 11:
                return None
            height = int.from_bytes(image[position + 5:position + 7], "big")
            width = int.from_bytes(image[position + 7:position + 9], "big")
            return (width, height) if width and height else None
        if marker == 0xda:
            return None
        position += 2 + size
    return None


def valid_jpeg(image):
    dimensions = jpeg_dimensions(image)
    return dimensions is not None and all(edge <= 1600 for edge in dimensions)


def list_images(db, key):
    source = meeting_retention.readable_source(db)
    return [{"id": identifier, "url": f"/api/sessions/{key}/images/{identifier}"}
            for identifier, in db.execute(
                f"""SELECT i.id FROM session_images i JOIN {source} s ON s.id=i.session_id
                    WHERE i.session_id=? ORDER BY i.rowid""", (key,))]


def add(db, key, image):
    if len(image) > MAX_BYTES or not valid_jpeg(image):
        raise ValueError("Choose a supported image up to 512 KB after compression.")
    source = meeting_retention.readable_source(db)
    if not db.execute(f"SELECT 1 FROM {source} WHERE id=? AND status IN ('complete','interrupted')", (key,)).fetchone():
        return None
    count = db.execute("SELECT count(*) FROM session_images WHERE session_id=?", (key,)).fetchone()[0]
    if count >= MAX_IMAGES:
        raise ValueError(f"Each recording can have up to {MAX_IMAGES} images.")
    identifier = str(uuid4())
    db.execute("INSERT INTO session_images VALUES(?, ?, ?)", (identifier, key, image))
    return {"id": identifier, "url": f"/api/sessions/{key}/images/{identifier}"}


def get(db, key, identifier):
    source = meeting_retention.readable_source(db)
    row = db.execute(f"""SELECT i.image FROM session_images i JOIN {source} s ON s.id=i.session_id
                         WHERE i.session_id=? AND i.id=?""", (key, identifier)).fetchone()
    return row["image"] if row else None


def handoff_folder(folder: Path):
    images = folder / ".agent-images"
    if folder.is_symlink() or images.is_symlink():
        raise OSError("The saved image folder is unavailable.")
    return images


def handoff_file(folder: Path, identifier: str):
    return handoff_folder(folder) / f"{UUID(identifier)}.jpg"


def export_for_agent(folder: Path, identifier: str, image: bytes):
    target = handoff_file(folder, identifier)
    folder.mkdir(mode=0o700, exist_ok=True)
    target.parent.mkdir(mode=0o700, exist_ok=True)
    output = NamedTemporaryFile(dir=target.parent, delete=False)
    temporary = Path(output.name)
    try:
        with output:
            output.write(image)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target.absolute()


def clear_handoff_files(folder: Path):
    images = handoff_folder(folder)
    if images.exists():
        shutil.rmtree(images)
