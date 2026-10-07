"""Public Python interface for macOS Harness."""

from .browser import BrowserHarness
from .guarded import GuardedApp, GuardedController, GuardedError
from .guarded_ax import NativeAXBackend
from .guarded_hybrid import NativeHybridBackend
from .macos import AccessibilityPermissionError, FocusChangedError, MacOS, MacOSError
from .ocr import VisionOCR

__all__ = [
    "AccessibilityPermissionError",
    "BrowserHarness",
    "FocusChangedError",
    "GuardedApp",
    "GuardedController",
    "GuardedError",
    "MacOS",
    "MacOSError",
    "NativeAXBackend",
    "NativeHybridBackend",
    "VisionOCR",
]
