"""python -m tophat  ->  dashboard (no args) or CLI subcommands (probe, run)"""

import sys

if __name__ == "__main__":
    if len(sys.argv) == 1:
        from tophat.ui.app import run_dashboard
        run_dashboard()
    else:
        from tophat.cli.main import cli_entry
        cli_entry()
