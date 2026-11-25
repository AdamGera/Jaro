"""Match OpenCode working directories to a folder or its Git repository."""

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat


PROJECT_TARGET = "project:"


def project_path(value, *, windows):
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or value.startswith(("//", "\\\\")):
        return None
    path = PureWindowsPath(value) if windows else PurePosixPath(value)
    if not path.is_absolute() or not 2 <= len(path.parts) <= 128 or ".." in path.parts:
        return None
    if windows:
        import ntpath

        def reserved(part):
            if hasattr(ntpath, "isreserved"):
                return ntpath.isreserved(part)
            return bool(PureWindowsPath(part).is_reserved() or part.endswith((".", " ")) or re.search(r'[<>:"|?*]', part))

        if not re.fullmatch(r"[A-Za-z]:", path.drive) or any(reserved(part) for part in path.parts[1:]):
            return None
    return path


def read_git_pointer(path):
    try:
        if not path.is_file():
            return ""
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return ""
            value = stream.read(4097)
        return value.decode().strip() if len(value) <= 4096 else ""
    except (OSError, UnicodeError, ValueError):
        return ""


def git_repository(folder, cache):
    visited = []
    result = None
    for directory in (folder, *folder.parents):
        if directory in cache:
            result = cache[directory]
            break
        visited.append(directory)
        marker = directory / ".git"
        try:
            if marker.is_dir():
                result = marker.resolve(strict=True)
                break
            pointer = read_git_pointer(marker)
            if pointer.startswith("gitdir: "):
                linked = directory / pointer.removeprefix("gitdir: ")
                if os.name == "nt" and not project_path(os.path.abspath(linked), windows=True):
                    continue
                linked = linked.resolve(strict=True)
                common = read_git_pointer(linked / "commondir")
                common_dir = linked / common if common else linked
                if os.name == "nt" and not project_path(os.path.abspath(common_dir), windows=True):
                    continue
                common_dir = common_dir.resolve(strict=True)
                if common_dir.is_dir():
                    result = common_dir
                    break
        except (OSError, RuntimeError, ValueError):
            continue
    cache.update((directory, result) for directory in visited)
    return result


def match_project_paths(target, directories):
    if not isinstance(directories, list) or len(directories) > 600:
        return []
    windows = os.name == "nt"
    folder = project_path(target, windows=windows)
    if folder is None or Path(folder) == Path.home():
        return []
    cache = {}
    repo = git_repository(Path(folder), cache)
    matches = []
    for index, value in enumerate(directories):
        directory = project_path(value, windows=windows)
        if directory is None:
            continue
        in_project = git_repository(Path(directory), cache) == repo if repo is not None else directory.is_relative_to(folder)
        if in_project:
            matches.append(index)
    return matches
