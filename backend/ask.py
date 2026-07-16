"""Compatibility entry point for asking ContextOS memory questions."""
import sys

from backend.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["ask", *sys.argv[1:]]))
