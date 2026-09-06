"""
CP2: frontmost-app detection.

    from active_app import get_active_app_name
    name = get_active_app_name()  # sanitized, or None on any failure
"""
import re


def sanitize(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-') or 'unknown'


def get_active_app_name() -> str | None:
    """Return the sanitized frontmost app name, or None on any failure."""
    try:
        from AppKit import NSWorkspace
        from Foundation import NSDate, NSRunLoop

        # NSWorkspace.frontmostApplication() is a notification-driven,
        # cached property: it only updates when the calling process's run
        # loop processes a pending NSWorkspaceDidActivateApplicationNotification.
        # A plain script never runs a run loop, so without pumping it briefly
        # here, this value is only accurate on the very first call and then
        # stays stuck on whatever app was frontmost at process start.
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        name = app.localizedName()
        if not name:
            return None
        return sanitize(name)
    except Exception:
        return None


if __name__ == "__main__":
    print(get_active_app_name())
