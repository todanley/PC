import sys

if sys.platform == "darwin":
    from .mac import Input, Screen
elif sys.platform == "win32":
    from .win import Input, Screen
else:
    raise RuntimeError(f"Unsupported platform: {sys.platform}")

__all__ = ["Input", "Screen"]
