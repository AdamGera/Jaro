"""Private image attachments for saved sessions."""
from uuid import uuid4

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


def valid_jpeg(image):
    """Check the normalized browser JPEG has a bounded frame."""
    if not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
        return False
    position = 2
    while position + 4 <= len(image) and image[position] == 0xff:
        marker = image[position + 1]
        size = int.from_bytes(image[position + 2:position + 4], "big")
        if size < 2 or position + 2 + size > len(image):
            return False
        if marker in (0xc0, 0xc2):
            if size < 11:
                return False
            height = int.from_bytes(image[position + 5:position + 7], "big")
            width = int.from_bytes(image[position + 7:position + 9], "big")
            return 0 < width <= 1600 and 0 < height <= 1600
        if marker == 0xda:
            return False
        position += 2 + size
    return False


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
