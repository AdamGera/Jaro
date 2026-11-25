import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from portable_terminal_origin import focused_terminal_context, linux_terminal_context


@contextmanager
def terminal_pane(folder):
    import pty
    import select
    import signal

    pid, master = pty.fork()
    if pid == 0:
        try:
            os.chdir(folder)
            os.write(sys.stdout.fileno(), b"ready\n")
            os.read(sys.stdin.fileno(), 1)
        finally:
            os._exit(0)
    try:
        if not select.select([master], [], [], 3)[0] or os.read(master, 64).strip() != b"ready":
            raise RuntimeError("The terminal pane failed to start.")
        yield
    finally:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
        os.close(master)


@unittest.skipUnless(sys.platform == "linux", "requires Linux procfs and a foreground PTY")
class PortableTerminalOriginTests(unittest.TestCase):
    def test_identifies_one_foreground_pty_and_declines_ambiguous_panes(self):
        binaries = (Path("/proc/self/exe").readlink().name,)
        with TemporaryDirectory() as directory:
            project = Path(directory) / "my-project"
            work = project / "src"
            work.mkdir(parents=True)
            (project / ".git").mkdir()
            with terminal_pane(work):
                self.assertEqual(linux_terminal_context(os.getpid(), binaries, "Project"),
                                 {"folder": str(work), "project": str(project)})
                self.assertIsNone(linux_terminal_context(os.getpid(), ("wrong-owner",), "Project"))
                with terminal_pane(project):
                    self.assertIsNone(linux_terminal_context(os.getpid(), binaries, "Project"))
            self.assertIsNone(linux_terminal_context(os.getpid(), binaries, "Project"))

    def test_title_only_focus_requires_the_same_accessibility_window(self):
        from wayland_field import FocusWindow

        window = FocusWindow(None, None, "Project", "foot")
        terminal = {"folder": "/work/project/src", "project": "/work/project"}
        target = (":1.25", "/window1")

        async def read_bus(reader, *, timeout):
            return await reader(object())

        for after, expected in ((target, terminal), ((":1.26", "/window2"), None), (None, None)):
            with (self.subTest(after=after),
                  patch("linux_call_pages.on_accessibility_bus", side_effect=read_bus),
                  patch("linux_call_pages.process_id", return_value=42),
                  patch("wayland_field.accessible_active_window", side_effect=[target, after]) as lookup,
                  patch("portable_terminal_origin.linux_terminal_context", return_value=terminal) as read):
                result = asyncio.run(focused_terminal_context(window, AsyncMock(return_value=window), "foot"))
                self.assertEqual(result, expected)
                read.assert_called_once_with(42, ("foot",), "Project")
                self.assertEqual(lookup.await_count, 2)
                self.assertTrue(all(call.kwargs["require_active"] is False for call in lookup.await_args_list))
