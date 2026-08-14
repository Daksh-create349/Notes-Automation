"""Clipboard fallback — copy notes to the clipboard with pyperclip.

Used when saving to Notion fails (bad token, page not shared, offline, etc.)
so the generated notes are never lost.
"""


import pyperclip


def save_to_clipboard(content: str) -> None:
    """Copy content to the system clipboard."""
    pyperclip.copy(content)
    print("📋 Notes copied to clipboard (Notion fallback).")
