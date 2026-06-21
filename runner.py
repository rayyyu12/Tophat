"""Shim: python runner.py -> tophat.cli run"""

if __name__ == "__main__":
    import sys
    sys.argv = ["tophat", "run", *sys.argv[1:]]
    from tophat.cli.main import cli_entry
    cli_entry()
