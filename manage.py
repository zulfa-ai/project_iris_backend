#!/usr/bin/env python
import os
import sys
from pathlib import Path

def main():
    # Add the "backend" directory to Python path so imports like
    # "iris_backend.urls" and "gameplay.urls" work.
    BACKEND_DIR = Path(__file__).resolve().parent
    sys.path.insert(0, str(BACKEND_DIR))

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "iris_backend.settings")

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Is your virtual environment activated?"
        ) from exc

    execute_from_command_line(sys.argv)

if __name__ == "__main__":
    main()
