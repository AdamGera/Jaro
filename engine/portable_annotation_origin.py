"""Read a destination scope from the focused portable desktop window."""
import asyncio
from contextlib import suppress
import re

from annotation_assets import TERMINAL_APPS, annotation_origin
from annotation_sites import SHARED_SITES, allowed_host, annotation_site
from portable_calls import BROWSER_CONTEXT_BINARIES
from portable_capture import CaptureError


BROWSER_ALIASES = {"org.mozilla.firefox", "com.google.chrome", "com.brave.browser", "org.chromium.chromium",
                   "com.microsoft.edge", "org.mozilla.firefoxesr", "io.gitlab.librewolf-community", "com.vivaldi.vivaldi"}
GENERAL_APPS = {"jaro", "jaro-native", "jaro-workspace", "jard-soundfolio-local", "jaro-call-surface", "jaro-pen",
                "applicationframehost", "explorer", "systemsettings"}


def portable_site(value):
    host = annotation_site(value.strip())
    if not host:
        return None
    host = host.removeprefix("www.")
    return host if allowed_host(host) and host not in SHARED_SITES else None


def portable_application(platform, value, *, allow_browser=False):
    app = value.strip().casefold()
    if platform == "windows":
        app = app.removesuffix(".exe")
    prefix = "windows" if platform == "windows" else "linux"
    if (app in GENERAL_APPS or app in TERMINAL_APPS or f"{prefix}.{app}" in TERMINAL_APPS
            or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*", app)):
        return None
    if not allow_browser and (app in BROWSER_CONTEXT_BINARIES or app in BROWSER_ALIASES):
        return None
    return app


def source_for_context(platform, context):
    name = context.application.strip()
    prefix = "windows" if platform == "windows" else "linux"
    app_id = f"{prefix}.{name.casefold().removesuffix('.exe') if platform == 'windows' else name.casefold()}"
    if app_id in TERMINAL_APPS and context.terminal:
        origin = annotation_origin({"app": app_id, "name": name, "terminal": context.terminal})
        return origin if origin and origin.get("terminal") else None
    app = portable_application(platform, name, allow_browser=True)
    if not app:
        return None
    if app in BROWSER_CONTEXT_BINARIES or app in BROWSER_ALIASES or context.host or context.site_url:
        if context.site_url:
            origin = annotation_origin({"app": f"{prefix}.{app}", "name": name, "siteUrl": context.site_url})
            if origin and origin.get("siteUrl"):
                return origin
        if host := portable_site(context.host):
            return annotation_origin({"app": f"{prefix}.{app}", "name": name, "site": host})
        return None
    return annotation_origin({"app": f"{prefix}.{app}", "name": name})


async def observed_annotation_origin(dictation):
    if dictation and dictation.platform in ("windows", "x11", "wayland"):
        with suppress(TimeoutError, OSError, ValueError, CaptureError):
            async with asyncio.timeout(.6):
                return source_for_context(dictation.platform, await dictation.output_context(read_site=True, annotation=True))
    return None
