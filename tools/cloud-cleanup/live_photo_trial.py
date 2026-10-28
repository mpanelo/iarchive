"""Entry point for one bounded disposable Live Photo trial."""

import sys

from recovery_trial import LIVE_PHOTO, main


if __name__ == "__main__":
    sys.exit(main(LIVE_PHOTO))
