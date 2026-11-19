"""Console output that cannot stop the program.

A player may type any name.  On Windows, output that is redirected (``> log.txt``, ``| Tee-Object``) is written
in a legacy code page such as cp1252, and ``print`` then raises UnicodeEncodeError for a name like a Tamil or
Chinese one.  The programs call ``tolerant_output()`` first, so such characters print as ``?`` instead.
"""
import sys


def tolerant_output() -> None:
    """Make stdout and stderr replace characters they cannot encode instead of raising."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)      # absent on StringIO and other stand-ins
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass
