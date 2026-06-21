"""TopHat entry point.

    python tophat.py            # launch the web dashboard (http://127.0.0.1:8800)
    python tophat.py tui        # legacy Textual terminal dashboard
    python tophat.py probe|run  # CLI commands
"""

import sys

if __name__ == "__main__":
    if len(sys.argv) == 1:
        from tophat.server.app import main
        main()
    elif sys.argv[1] == "tui":
        from tophat.ui.app import run_dashboard
        run_dashboard()
    else:
        from tophat.cli.main import cli_entry
        cli_entry()
