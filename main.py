"""Notes Automation — CLI entry point.

Records audio, transcribes it via Groq, generates notes and quizzes,
and saves everything to Notion (with a clipboard fallback).
"""

import argparse
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / ".env"
TRANSCRIPTS_DIR = Path(tempfile.gettempdir()) / "notes_automation_transcripts"
_LEGACY_TRANSCRIPT = Path(tempfile.gettempdir()) / "notes_automation_last_transcript.txt"
_MAX_SAVED = 10


def _migrate_legacy() -> None:
    """Move the old single-file transcript into the new directory if it exists."""
    if not _LEGACY_TRANSCRIPT.exists():
        return
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    dest = TRANSCRIPTS_DIR / f"00000000_000000_legacy.txt"
    try:
        dest.write_text(_LEGACY_TRANSCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        _LEGACY_TRANSCRIPT.unlink(missing_ok=True)
    except Exception:
        pass


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


def _save_transcript(transcript: str) -> Path:
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Extract only ASCII words for a clean filename, or default to 'lecture_transcript'
    ascii_words = [re.sub(r"[^a-zA-Z0-9]", "", w) for w in transcript.split()[:8]]
    label = "_".join(w for w in ascii_words if w)[:30]
    filename = f"{timestamp}_{label}.txt" if label else f"{timestamp}_lecture_transcript.txt"
    path = TRANSCRIPTS_DIR / filename
    path.write_text(transcript, encoding="utf-8")
    all_files = sorted(TRANSCRIPTS_DIR.glob("*.txt"))
    for old in all_files[:-_MAX_SAVED]:
        old.unlink(missing_ok=True)
    return path


def _list_transcripts() -> list[Path]:
    if not TRANSCRIPTS_DIR.exists():
        return []
    return sorted(TRANSCRIPTS_DIR.glob("*.txt"), reverse=True)


def cmd_start() -> None:
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
    from recorder import stop_recording
    _process(stop_recording(), clipboard=clipboard)


def cmd_process(audio_path: str, clipboard: bool = False) -> None:
    """Run the full pipeline on an existing audio file."""
    p = Path(audio_path)
    if not p.exists():
        print(f"File not found: {audio_path}")
        return
    _process(str(p), clipboard=clipboard)


def cmd_retry(clipboard: bool = False) -> None:
    _migrate_legacy()
    files = _list_transcripts()
    if not files:
        print("No saved transcripts found. Run 'start' first to record a lecture.")
        return

    print("Saved transcripts:\n")
    for i, f in enumerate(files, 1):
        parts = f.stem.split("_", 2)
        try:
            dt = datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S")
            date_str = dt.strftime("%d %b %Y  %H:%M:%S")
        except Exception:
            date_str = f.stem

        preview = ""
        try:
            first_line = f.read_text(encoding="utf-8").split("\n")[0].strip()
            # Clean any non-Latin or foreign characters from the terminal list preview
            cleaned_line = re.sub(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF\u0900-\u097F]+", "", first_line).strip()
            if cleaned_line:
                preview = f"  —  {cleaned_line[:60]}..." if len(cleaned_line) > 60 else f"  —  {cleaned_line}"
        except Exception:
            pass

        size = f.stat().st_size
        print(f"  [{i}] {date_str}  ({size} chars){preview}")

    print()
    try:
        choice = input(f"Pick a transcript [1-{len(files)}]: ").strip()
        idx = int(choice) - 1
        if not (0 <= idx < len(files)):
            print("Invalid choice.")
            return
    except (ValueError, EOFError):
        print("Invalid input.")
        return
    except KeyboardInterrupt:
        print("\nCancelled.")
        return

    selected = files[idx]
    transcript = selected.read_text(encoding="utf-8").strip()
    if not transcript:
        print("Selected transcript is empty.")
        return

    print(f"\nUsing: {selected.name}\n")
    _notes_from_transcript(transcript, stem=selected.stem, clipboard=clipboard)


def _process(audio_path: str | None, clipboard: bool) -> None:
    if not audio_path:
        print("No audio recorded.")
        return

    try:
        from transcriber import transcribe_audio
        print("\n🎧 Transcribing audio...")
        transcript = transcribe_audio(audio_path)
        if not transcript or not transcript.strip():
            print("Error: transcription returned nothing.")
            return
        
        words = transcript.strip().split()
        if len(words) < 8:
            print(f"⚠️ Audio was silent or too short ({len(words)} words detected). Skipping note generation.")
            return

        saved = _save_transcript(transcript)
        print(f"✅ Audio transcribed ({len(words)} words captured).")
    except Exception as e:
        print(f"Transcription error: {e}")
        return

    _notes_from_transcript(transcript, stem=Path(audio_path).stem, clipboard=clipboard)

    try:
        Path(audio_path).unlink(missing_ok=True)
    except Exception as e:
        print(f"Warning: could not delete audio file: {e}")


def _notes_from_transcript(transcript: str, stem: str = "notes", clipboard: bool = False) -> None:
    summary: dict = {"notes": None, "notion_url": None, "clipboard": False}

    try:
        from note_generator import generate_notes
        print("🧠 Translating and generating comprehensive English study notes...")
        notes = generate_notes(transcript)
        if not notes:
            print("Error: note generation returned nothing.")
            return
        print(f"\n==================== 📚 GENERATED STUDY NOTES ====================\n")
        print(notes)
        print(f"\n===================================================================\n")
    except Exception as e:
        print(f"Note generation error: {e}")
        return

    notes_path = Path(tempfile.gettempdir()) / f"{stem}_notes.md"
    try:
        notes_path.write_text(notes, encoding="utf-8")
        summary["notes"] = str(notes_path)
    except Exception as e:
        print(f"Warning: could not write notes file: {e}")

    quiz: str | None = None
    try:
        from quiz_generator import generate_quiz
        print("Generating quiz...")
        quiz = generate_quiz(notes)
        if not quiz:
            print("Warning: quiz generation returned nothing.")
    except Exception as e:
        print(f"Quiz generation error: {e}")

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

    if clipboard or not notion_url:
        try:
            from clipboard_saver import save_to_clipboard
            combined = notes + ("\n\n---\n\n" + quiz if quiz else "")
            save_to_clipboard(combined)
            summary["clipboard"] = True
        except Exception as e:
            print(f"Clipboard error: {e}")

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

    retry_parser = subparsers.add_parser(
        "retry",
        help="Pick from saved transcripts and re-run notes generation.",
    )
    retry_parser.add_argument(
        "--clipboard",
        action="store_true",
        help="Skip Notion and copy notes + quiz to clipboard instead.",
    )

    process_parser = subparsers.add_parser(
        "process",
        help="Run the full pipeline on an existing audio file (transcribe → notes → Notion).",
    )
    process_parser.add_argument("file", help="Path to the audio file (.wav, .mp3, etc.)")
    process_parser.add_argument(
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
    elif args.command == "retry":
        cmd_retry(clipboard=getattr(args, "clipboard", False))
    elif args.command == "process":
        cmd_process(args.file, clipboard=getattr(args, "clipboard", False))


if __name__ == "__main__":
    main()
