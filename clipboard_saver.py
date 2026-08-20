"""Clipboard fallback — copy notes to the clipboard with pyperclip.

Used when saving to Notion fails (bad token, page not shared, offline, etc.)
or when running in clipboard-only mode.
"""


def save_to_clipboard(content: str, title: str | None = None) -> bool:
    """Copy content to the system clipboard."""
    if not content or not content.strip():
        return False

    copied = False
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(content)
        print("📋 Notes copied to system clipboard.")
        copied = True
    except Exception as e:
        print(f"  Note: clipboard utility unavailable ({e}).")

    return copied


