# Regular package marker. test_tc_observe.py imports helpers via
# `tests.test_tc_apply`; without this file a stray `tests` REGULAR package in
# site-packages beats the local NAMESPACE portion (regular packages win the
# scan regardless of sys.path order) and the import breaks.
