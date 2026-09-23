"""Public Python interface for macOS Harness."""

from .browser import BrowserHarness
from .guarded import GuardedController, GuardedError
from .guarded_ax import NativeAXBackend
from .macos import AccessibilityPermissionError, FocusChangedError, MacOS, MacOSError

__all__ = [
    "AccessibilityPermissionError",
    "BrowserHarness",
    "FocusChangedError",
    "GuardedController",
    "GuardedError",
    "MacOS",
    "MacOSError",
    "NativeAXBackend",
]
