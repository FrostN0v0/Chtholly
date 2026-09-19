"""Start Chtholly after validating its selected configuration and resources."""

import sys

sys.dont_write_bytecode = True

from utils.startup.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
