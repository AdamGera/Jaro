"""Display geometry for a native X11 desktop."""

from contextlib import closing, suppress
import os

from portable_capture import CaptureError


def x11_session():
    return bool(os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
                and os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland")


def recording_target(area=None):
    from Xlib import display, error

    try:
        with closing(display.Display()) as connection:
            root = connection.screen().root
            geometry = root.get_geometry()
            width, height = geometry.width, geometry.height
            if width < 2 or height < 2:
                raise CaptureError("The X11 display is too small to record.")

            monitors = []
            if connection.has_extension("RANDR"):
                with suppress(AttributeError, error.XError):
                    monitors = root.xrandr_get_monitors(True).monitors

            if area is not None:
                from screen_draft import validate_region

                x, y, area_width, area_height = area
                validate_region(area, (width, height))
                if area_width % 2 or area_height % 2 or area_width > 2560 or area_height > 1440:
                    raise ValueError("Use even dimensions up to 2560 by 1440 pixels for an exact recording area.")
                if monitors and not any(item.x <= x and item.y <= y
                                        and x + area_width <= item.x + item.width_in_pixels
                                        and y + area_height <= item.y + item.height_in_pixels for item in monitors):
                    raise ValueError("Choose an area within a single X11 monitor.")
                return tuple(area)

            if monitors:
                with suppress(error.XError):
                    pointer = root.query_pointer()
                    chosen = next((item for item in monitors
                                   if item.x <= pointer.root_x < item.x + item.width_in_pixels
                                   and item.y <= pointer.root_y < item.y + item.height_in_pixels), None)
                    if chosen is None:
                        chosen = next((item for item in monitors if item.primary), None)
                    if chosen is not None:
                        x, y = max(0, chosen.x), max(0, chosen.y)
                        right = min(width, chosen.x + chosen.width_in_pixels)
                        bottom = min(height, chosen.y + chosen.height_in_pixels)
                        if right - x >= 2 and bottom - y >= 2:
                            return x, y, right - x, bottom - y
            return 0, 0, width, height
    except (error.DisplayError, error.ConnectionClosedError, error.XError, OSError) as failure:
        raise CaptureError("Could not open the X11 display for recording.") from failure
