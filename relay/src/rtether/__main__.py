"""`python -m rtether`: same as the `rtether` command, without its unsigned .exe launcher."""

import sys

from rtether.cli import main

sys.exit(main())
