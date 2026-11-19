"""Forgot the site password? Set a new one from the terminal:   python -m app.password
(Messages are in English: Termux shows Hebrew reversed.)"""
import getpass
import sys

from .config import Settings


def main() -> int:
    print("\n  Set a NEW password for the site (the old one is not needed).")
    print("  Tip: use English letters and numbers, at least 4 characters.\n")
    for _ in range(3):
        first = getpass.getpass("  New password: ").strip()
        if len(first) < 4:
            print("  Too short - at least 4 characters. Try again.\n")
            continue
        if getpass.getpass("  Type it again: ").strip() != first:
            print("  The two did not match. Try again.\n")
            continue
        Settings().set_password(first)
        print("\n  *** Done! The new password is saved. ***")
        print("  If the site is running, stop it (CTRL then c) and start it again: tv\n")
        return 0
    print("\n  Nothing was changed.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
