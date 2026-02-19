#!/usr/bin/env python3
"""
Entry point: python main.py [--once] [-d SECONDI]
Esegue la conversazione vocale.
"""

import sys
KNOWN = {"run", "test", "list-devices", "list_devices"}
argv = sys.argv[1:]
if not argv or argv[0] not in KNOWN:
    sys.argv = ["talk-module", "run"] + argv
else:
    sys.argv = ["talk-module"] + argv

from talk_module.cli import main
exit(main())
