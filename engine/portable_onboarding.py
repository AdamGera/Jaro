"""Track the portable dictation tour independently of model and shortcut settings."""
import asyncio
from uuid import uuid4


class PortableOnboarding:
    def __init__(self, root):
        self.marker = root / ".dictation-setup-done"
        self.id = None if self.marker.is_file() else str(uuid4())
        self.changed = asyncio.Event()

    def publish(self, identifier):
        self.id = identifier
        self.changed.set()
        self.changed = asyncio.Event()

    def restart(self):
        self.marker.unlink(missing_ok=True)
        self.publish(str(uuid4()))
        return self.id

    def finish(self, identifier):
        if self.id != identifier:
            return False
        try:
            with self.marker.open("x"):
                pass
        except FileExistsError:
            if not self.marker.is_file():
                raise
        self.publish(None)
        return True
