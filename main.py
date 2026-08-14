"""Notes Automation — CLI entry point.

Records audio, transcribes it via Groq, generates notes and quizzes,
and saves everything to Notion (with a clipboard fallback).
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / ".env"


def check_env() -> bool:
    if not ENV_PATH.exists():
        print(
            "Error: .env file not found.\n"
            "Copy .env.example to .env and fill in your keys:\n"
            "  cp .env.example .env"
        )
        return False

    load_dotenv(ENV_PATH)

    required = ["GROQ_API_KEY", "NOTION_TOKEN", "NOTION_PAGE_ID"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        print(f"Error: missing environment variables: {', '.join(missing)}")
        return False

    return True


def cmd_start() -> None:
    """Begin recording; block until Ctrl+C, then hand off to cmd_stop."""
    from recorder import start_recording, stop_recording

    start_recording()
    print("Recording... Press Ctrl+C to stop.")

    try:
        import time
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()

    _process(stop_recording(), clipboard=False)


def cmd_stop(clipboard: bool = False) -> None:
    """Stop an in-progress recording and process it."""
    from recorder import stop_recording
    _process(stop_recording(), clipboard=clipboard)


def _process(audio_path: str | None, clipboard: bool) -> None:
    """Transcribe → notes → quiz → save. Cleans up audio when done."""
    summary: dict = {
        "audio": audio_path,
        "notes": None,
        "notion_url": None,
        "clipboard": False,
    }

    if not audio_path:
        print("No audio recorded.")
        return

    # --- Transcribe ---
    try:
        from transcriber import transcribe_audio
        print("\nTranscribing...")
        transcript = transcribe_audio(audio_path)
        if not transcript:
            print("Error: transcription returned nothing.")
            return
        print(f"Transcript:\n{transcript}\n")
    except Exception as e:
        print(f"Transcription error: {e}")
        return

    # --- Generate notes ---
    try:
        from note_generator import generate_notes
        print("Generating notes...")
        notes = generate_notes(transcript)
        if not notes:
            print("Error: note generation returned nothing.")
            return
        print(f"\nNotes:\n{notes}\n")
    except Exception as e:
        print(f"Note generation error: {e}")
        return

    # Save notes to temp file
    notes_path = Path(tempfile.gettempdir()) / (Path(audio_path).stem + "_notes.md")
    try:
        notes_path.write_text(notes, encoding="utf-8")
        summary["notes"] = str(notes_path)
    except Exception as e:
        print(f"Warning: could not write notes file: {e}")

    # --- Generate quiz ---
    quiz: str | None = None
    try:
        from quiz_generator import generate_quiz
        print("Generating quiz...")
        quiz = generate_quiz(notes)
        if quiz:
            print(f"\nQuiz:\n{quiz}\n")
        else:
            print("Warning: quiz generation returned nothing.")
    except Exception as e:
        print(f"Quiz generation error: {e}")

    # --- Save to Notion (unless --clipboard forced) ---
    notion_url: str | None = None
    if not clipboard:
        try:
            from notion_saver import save_notes_to_notion
            title = notes.splitlines()[0].lstrip("# ").strip() or "Lecture Notes"
            print("Saving to Notion...")
            notion_url = save_notes_to_notion(title, notes)
            summary["notion_url"] = notion_url
        except Exception as e:
            print(f"Notion save error: {e}")

        if notion_url and quiz:
            try:
                from quiz_generator import append_quiz_to_notion
                page_id = notion_url.rstrip("/").split("/")[-1].split("?")[0]
                append_quiz_to_notion(page_id, quiz)
            except Exception as e:
                print(f"Quiz Notion append error: {e}")

    # --- Clipboard fallback ---
    if clipboard or not notion_url:
        try:
            from clipboard_saver import save_to_clipboard
            combined = notes + ("\n\n---\n\n" + quiz if quiz else "")
            save_to_clipboard(combined)
            summary["clipboard"] = True
        except Exception as e:
            print(f"Clipboard error: {e}")

    # --- Clean up audio ---
    try:
        Path(audio_path).unlink(missing_ok=True)
    except Exception as e:
        print(f"Warning: could not delete audio file: {e}")

    # --- Final summary ---
    print("\n--- Summary ---")
    if summary["notes"]:
        print(f"Notes file : {summary['notes']}")
    if summary["notion_url"]:
        print(f"Notion URL : {summary['notion_url']}")
    if summary["clipboard"]:
        print("Clipboard  : notes + quiz copied")
    print("Done.")


def _welcome() -> None:
    width = 62
    border = "+" + "-" * width + "+"
    def row(text: str = "") -> str:
        return f"|{text.center(width)}|"

    print(border)
    print(row())
    print(row("LECTURE NOTES AUTOMATION"))
    print(row("Transcribe  |  Notes  |  Quiz  |  Notion"))
    print(row())
    print(border)
    print(row())
    print(row("Powered by  Groq  Whisper  +  LLaMA 3.3"))
    print(row("Saves to    Notion  (clipboard fallback)"))
    print(row())
    print(border)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="notes-automation",
        description="Record a lecture, transcribe it, generate notes and a quiz, save to Notion.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "start",
        help="Start recording — press Ctrl+C to stop and process automatically.",
    )

    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop an active recording and process it.",
    )
    stop_parser.add_argument(
        "--clipboard",
        action="store_true",
        help="Skip Notion and copy notes + quiz to clipboard instead.",
    )

    _welcome()

    args = parser.parse_args()

    if not check_env():
        sys.exit(1)

    if args.command == "start":
        cmd_start()
    elif args.command == "stop":
        cmd_stop(clipboard=getattr(args, "clipboard", False))


if __name__ == "__main__":
    main()
