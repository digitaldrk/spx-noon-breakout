"""CLI: python -m spx_noon | python -m spx_noon check | python -m spx_noon wrap."""
from __future__ import annotations

import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "check":
        from .check import main as check_main
        return check_main(argv[1:])
    if argv and argv[0] == "wrap":
        from .wrap_run import main as wrap_main
        return wrap_main(argv[1:])
    from .runner import main as runner_main
    return runner_main(argv)


if __name__ == "__main__":
    sys.exit(main())
