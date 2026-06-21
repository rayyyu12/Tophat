"""Shim: python probe.py -> tophat probe"""

if __name__ == "__main__":
    import sys
    sys.argv = ["tophat", "probe", *sys.argv[1:]]
    from tophat.cli.main import cli_entry
    cli_entry()
