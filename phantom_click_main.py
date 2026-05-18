"""PyInstaller entry-point wrapper.

PyInstaller runs the entry script as `__main__` with no parent package, which
breaks relative imports inside `app/`. This trivial wrapper imports the
package so `from .ui import …` style imports resolve normally.

For operator-side dev, keep using `python3 -m app.main`.
"""
from app.main import main

if __name__ == "__main__":
    main()
