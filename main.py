"""Main CLI Dispatcher for Notes-Automation.

Subcommands:
  - start            : Record lecture, transcribe in 5-min chunks, synthesize notes in 15-min blocks, upload to Notion
  - stop             : Safely signal an active recording process from another terminal
  - resume           : Resume an interrupted session from state.json without repeating completed work
  - transcribe-only  : Record or process audio, transcribe & cache chunks to disk, then stop
  - generate-only    : Read cached transcript chunks, synthesize notes & quiz, upload to Notion
  - estimate         : Dry-run quota feasibility analyzer for 2-8+ hour lectures
"""

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from clipboard_saver import save_to_clipboard
from estimator import print_lecture_estimate
from note_generator import generate_all_notes
from notion_saver import save_notes_to_notion
from quiz_generator import append_quiz_to_notion, generate_quiz
from recorder import check_crash_recovery, is_recording, start_recording, stop_recording
from section_store import SectionStore
from session_manager import (
    Session,
    SessionStatus,
    clear_active_pointer,
    create_new_session,
    find_resumable_sessions,
    load_active_session,
)
from transcript_store import TranscriptStore
from transcriber import transcribe_audio_pipeline


ENV_PATH = Path(__file__).parent / ".env"


def _welcome():
    print(r"""
+--------------------------------------------------------------+
|                                                              |
|                   LECTURE NOTES AUTOMATION                   |
|           Transcribe  |  Notes  |  Quiz  |  Notion           |
|                                                              |
+--------------------------------------------------------------+
|                                                              |
|         Powered by  Groq Whisper-Turbo  +  Fast LLM          |
|           Saves to    Notion  (clipboard fallback)           |
|                                                              |
+--------------------------------------------------------------+
""")


def check_env(require_notion: bool = True) -> bool:
    """Validate that required environment variables are set."""
    if not ENV_PATH.exists():
        print(
            "Error: .env file not found.\n"
            "Copy .env.example to .env and fill in your keys:\n"
            "  cp .env.example .env"
        )
        return False

    load_dotenv(ENV_PATH)

    required = ["GROQ_API_KEY"]
    if require_notion:
        required += ["NOTION_TOKEN", "NOTION_PAGE_ID"]

    missing = [v for v in required if not os.getenv(v)]
    if missing:
        print(f"Error: missing environment variables: {', '.join(missing)}")
        if "NOTION_TOKEN" in missing or "NOTION_PAGE_ID" in missing:
            print("Tip: You can also use --clipboard to generate notes without Notion credentials.")
        return False

    return True


def _run_pipeline(
    session: Session,
    clipboard_only: bool = False,
    keep_audio: bool = False,
    chunk_minutes: int = 5,
    chunks_per_block: int = 3,
    model: str | None = None,
    resume: bool = False,
) -> None:
    """Execute end-to-end processing pipeline for a session."""
    transcript_store = TranscriptStore(session.transcripts_dir)
    section_store = SectionStore(session.sections_dir)

    try:
        # Stage 1: Transcription
        session.update_status(SessionStatus.TRANSCRIBING)
        print("\n" + "=" * 60)
        print("STAGE 1: AUDIO CHUNKING & WHISPER TRANSCRIPTION")
        print("=" * 60)

        chunks = transcribe_audio_pipeline(
            audio_path=session.audio_file,
            transcript_store=transcript_store,
            chunk_minutes=chunk_minutes,
            resume=resume,
        )

        if not chunks:
            print("⚠️ Transcription produced no text. Aborting pipeline.")
            return

        # Purge raw audio from disk immediately after transcription completes unless keep_audio is True
        if not keep_audio:
            session.cleanup_audio()
            print("🧹 Raw audio purged from local disk (transcripts safely cached).")

        session.save_state({
            "total_audio_chunks": len(chunks),
            "transcribed_chunks": len(chunks),
        })

        # Stage 2: Knowledge Synthesis (LLM Blocks)
        session.update_status(SessionStatus.NOTE_GENERATION)
        print("\n" + "=" * 60)
        print(f"STAGE 2: KNOWLEDGE SYNTHESIS ({chunks_per_block} chunks ≈ {chunks_per_block * chunk_minutes} min / block)")
        print("=" * 60)

        notes_result = generate_all_notes(
            transcript_chunks=chunks,
            section_store=section_store,
            chunks_per_block=chunks_per_block,
            model=model,
            resume=resume,
        )

        if not notes_result or not notes_result.get("blocks"):
            print("⚠️ Note generation produced no content. Skipping Notion upload.")
            return

        all_sections = section_store.get_all_sections()
        session.save_state({"generated_sections": len(all_sections)})

        # Stage 3: Quiz Generation (Single Pass)
        session.update_status(SessionStatus.QUIZ_GENERATION)
        print("\n" + "=" * 60)
        print("STAGE 3: CONCEPTUAL QUIZ GENERATION (SINGLE FINAL PASS)")
        print("=" * 60)

        # Assemble text outline for quiz
        notes_text_summary = "\n\n".join(
            f"### Section {s.section_id}: {s.title}\n" + "\n".join(str(b.get("text", "")) for b in s.blocks if b.get("type") in ("heading_1", "heading_2", "paragraph", "callout"))
            for s in all_sections
        )
        quiz_md = generate_quiz(notes_text_summary, model=model)
        if quiz_md:
            session.save_state({"quiz_generated": True})
            print("✅ 5-question conceptual review quiz generated.")

        # Stage 4: Notion Upload / Clipboard Fallback
        notion_url = None
        if not clipboard_only:
            session.update_status(SessionStatus.NOTION_UPLOAD)
            print("\n" + "=" * 60)
            print("STAGE 4: NOTION UPLOAD & RICH TYPOGRAPHY")
            print("=" * 60)

            notion_url = save_notes_to_notion(
                title=session.lecture_title,
                content=notes_result,
            )

            if notion_url and quiz_md:
                import re
                page_id_match = re.search(r"([0-9a-fA-F]{32})", notion_url.replace("-", ""))
                if page_id_match:
                    append_quiz_to_notion(page_id_match.group(1), quiz_md)

        # Guaranteed clipboard copy
        full_markdown = section_store.get_all_markdown()
        if quiz_md:
            full_markdown += f"\n\n---\n\n## 📝 Lecture Review Quiz\n\n{quiz_md}"

        save_to_clipboard(full_markdown, title=session.lecture_title)
        session.update_status(SessionStatus.COMPLETE, notion_url=notion_url)

        print("\n" + "=" * 60)
        print("🎉 PIPELINE COMPLETE!")
        if notion_url:
            print(f"📖 Notion Page : {notion_url}")
        print("=" * 60)

    except KeyboardInterrupt:
        print("\n⚠️ Process interrupted by user. Run './lec resume' to continue from saved checkpoints.")
    except Exception as e:
        print(f"\n❌ Pipeline error: {e}")
        session.update_status(SessionStatus.ERROR, error=str(e))
    finally:
        if not keep_audio:
            session.cleanup()
        else:
            print(f"💾 Audio preserved locally at: {session.audio_file}")
            clear_active_pointer()


def cmd_start(args: argparse.Namespace) -> None:
    """Start live recording session."""
    title = getattr(args, "lecture_name", None)
    if not title or not title.strip():
        try:
            while not title:
                entered = input("📝 Enter lecture name: ").strip()
                if entered:
                    title = entered
                else:
                    print("  ⚠️ Lecture name cannot be empty. Please enter a name.")
        except (KeyboardInterrupt, EOFError):
            print("\n❌ Session cancelled.")
            return

    title = title.strip()
    session = create_new_session(lecture_title=title)

    print(f"\n🎙️  Starting session: {session.session_dir.name}")
    print(f"   Lecture Title: '{session.lecture_title}'")
    
    started = start_recording(
        output_path=session.audio_file,
        autosave_dir=session.autosave_dir,
    )
    if not started:
        session.cleanup()
        return

    try:
        while is_recording():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n⏹️  Stopping recording...")

    audio_path = stop_recording()
    if not audio_path or not os.path.exists(audio_path):
        print("⚠️ No audio captured. Session cancelled.")
        session.cleanup()
        return

    _run_pipeline(
        session=session,
        clipboard_only=getattr(args, "clipboard", False),
        keep_audio=getattr(args, "keep_audio", False),
        chunk_minutes=args.chunk_minutes,
        chunks_per_block=args.chunks_per_block,
        model=getattr(args, "model", None),
        resume=False,
    )


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop active recording safely across processes."""
    active = load_active_session()
    if not active:
        print("⚠️ No active recording session found to stop.")
        return

    state = active.load_state()
    pid = state.get("pid")
    
    if pid and pid != os.getpid():
        try:
            print(f"📡 Signaling active recording process (PID: {pid}) to stop gracefully...")
            os.kill(pid, signal.SIGINT)
            print("✅ Stop signal sent. The recording terminal is now processing notes.")
            return
        except ProcessLookupError:
            print(f"  Note: Process {pid} already finished.")
        except Exception as e:
            print(f"⚠️ Could not signal process {pid}: {e}")

    # Fallback if in same process
    if is_recording():
        stop_recording()


def cmd_resume(args: argparse.Namespace) -> None:
    """Resume interrupted session from state.json without repeating work."""
    sessions = find_resumable_sessions()
    if not sessions:
        print("ℹ️ No resumable sessions found.")
        return

    session = sessions[0]
    state = session.load_state()
    print(f"\n🔄 Resuming session '{session.session_dir.name}' (Title: '{session.lecture_title}')")
    print(f"   Last Status: {state.get('status')} | Transcribed Chunks: {state.get('transcribed_chunks', 0)}")

    _run_pipeline(
        session=session,
        clipboard_only=getattr(args, "clipboard", False),
        keep_audio=getattr(args, "keep_audio", False),
        chunk_minutes=args.chunk_minutes,
        chunks_per_block=args.chunks_per_block,
        model=getattr(args, "model", None),
        resume=True,
    )


def cmd_transcribe_only(args: argparse.Namespace) -> None:
    """Record or take existing audio, transcribe 5-minute chunks, and cache to disk."""
    audio_path = getattr(args, "audio", None)
    if audio_path:
        if not os.path.exists(audio_path):
            print(f"❌ Error: Audio file '{audio_path}' not found.")
            return
        title = Path(audio_path).stem
        session = create_new_session(lecture_title=title)
        # Copy or point audio
        import shutil
        shutil.copy2(audio_path, session.audio_file)
    else:
        title = getattr(args, "lecture_name", None)
        if not title or not title.strip():
            try:
                while not title:
                    entered = input("📝 Enter lecture name: ").strip()
                    if entered:
                        title = entered
                    else:
                        print("  ⚠️ Lecture name cannot be empty. Please enter a name.")
            except (KeyboardInterrupt, EOFError):
                print("\n❌ Session cancelled.")
                return
        title = title.strip()
        session = create_new_session(lecture_title=title)
        print(f"🎙️  Recording for transcribe-only... (Press Ctrl+C to stop)")
        start_recording(output_path=session.audio_file, autosave_dir=session.autosave_dir)
        try:
            while is_recording():
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n⏹️  Stopping recording...")
        stop_recording()

    store = TranscriptStore(session.transcripts_dir)
    chunks = transcribe_audio_pipeline(
        audio_path=session.audio_file,
        transcript_store=store,
        chunk_minutes=args.chunk_minutes,
        resume=getattr(args, "resume", False),
    )

    if not getattr(args, "keep_audio", False):
        session.cleanup_audio()
        print("🧹 Raw audio purged from local disk (transcripts safely stored in JSON).")

    print(f"\n✅ Transcribe-only complete. Saved {len(chunks)} transcript chunks to: {session.transcripts_dir}")
    print(f"💡 You can generate notes later using: ./lec generate-only --session-dir {session.session_dir}")


def cmd_generate_only(args: argparse.Namespace) -> None:
    """Generate notes and quiz from pre-cached transcript chunks."""
    s_dir = getattr(args, "session_dir", None)
    if s_dir and Path(s_dir).exists():
        session = Session(Path(s_dir))
    else:
        sessions = find_resumable_sessions()
        if not sessions:
            print("❌ Error: No session found with cached transcript chunks.")
            return
        session = sessions[0]

    store = TranscriptStore(session.transcripts_dir)
    chunks = store.get_all_completed()
    if not chunks:
        print(f"❌ Error: No completed transcript chunks found in {session.transcripts_dir}")
        return

    sec_store = SectionStore(session.sections_dir)
    notes_result = generate_all_notes(
        transcript_chunks=chunks,
        section_store=sec_store,
        chunks_per_block=args.chunks_per_block,
        model=getattr(args, "model", None),
        resume=getattr(args, "resume", False),
    )

    if not notes_result:
        return

    quiz_md = generate_quiz(sec_store.get_all_markdown(), model=getattr(args, "model", None))
    notion_url = None

    if not getattr(args, "clipboard", False):
        notion_url = save_notes_to_notion(session.lecture_title, notes_result)
        if notion_url and quiz_md:
            import re
            page_id_match = re.search(r"([0-9a-fA-F]{32})", notion_url.replace("-", ""))
            if page_id_match:
                append_quiz_to_notion(page_id_match.group(1), quiz_md)

    save_to_clipboard(sec_store.get_all_markdown() + (f"\n\n{quiz_md}" if quiz_md else ""), title=session.lecture_title)
    session.update_status(SessionStatus.COMPLETE, notion_url=notion_url)
    print(f"✅ Generate-only complete. Uploaded to Notion: {notion_url}")


def main():
    parser = argparse.ArgumentParser(
        prog="notes-automation",
        description="Record a lecture, transcribe it in 5-min chunks, synthesize notes in 15-min blocks, save to Notion.",
    )

    env_chunk_min = int(os.getenv("AUDIO_CHUNK_MINUTES", os.getenv("CHUNK_MINUTES", "5")))
    env_chunks_per_block = int(os.getenv("LLM_CHUNKS_PER_BLOCK", os.getenv("CHUNKS_PER_BLOCK", "3")))

    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("--model", type=str, default=None, help="LLM model name.")
    common_parser.add_argument("--chunk-minutes", type=int, default=env_chunk_min, help=f"Audio chunk minutes (default: {env_chunk_min}).")
    common_parser.add_argument("--chunks-per-block", type=int, default=env_chunks_per_block, help=f"LLM chunks grouped per block (default: {env_chunks_per_block}).")
    common_parser.add_argument("--keep-audio", action="store_true", default=False, help="Preserve audio file locally after completion.")
    common_parser.add_argument("--resume", action="store_true", default=False, help="Resume from cached checkpoints.")
    common_parser.add_argument("--hours", type=float, default=None, help="Lecture duration in hours for estimation.")
    common_parser.add_argument("--minutes", type=float, default=None, help="Lecture duration in minutes for estimation.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # start
    p_start = subparsers.add_parser("start", parents=[common_parser], help="Start recording — press Ctrl+C to stop and process.")
    p_start.add_argument("--clipboard", action="store_true", help="Skip Notion and copy to clipboard.")
    p_start.add_argument("--name", "--title", dest="lecture_name", type=str, default=None, help="Lecture title.")

    # stop
    p_stop = subparsers.add_parser("stop", parents=[common_parser], help="Stop active recording safely.")

    # resume
    p_resume = subparsers.add_parser("resume", parents=[common_parser], help="Resume interrupted session from state.json.")
    p_resume.add_argument("--clipboard", action="store_true", help="Copy to clipboard.")

    # transcribe-only
    p_trans = subparsers.add_parser("transcribe-only", parents=[common_parser], help="Transcribe & cache chunks to disk.")
    p_trans.add_argument("--audio", type=str, default=None, help="Path to audio file.")
    p_trans.add_argument("--name", "--title", dest="lecture_name", type=str, default=None, help="Lecture title.")

    # generate-only
    p_gen = subparsers.add_parser("generate-only", parents=[common_parser], help="Generate notes & quiz from cached chunks.")
    p_gen.add_argument("--session-dir", type=str, default=None, help="Session directory.")
    p_gen.add_argument("--clipboard", action="store_true", help="Copy to clipboard.")

    # estimate
    subparsers.add_parser("estimate", parents=[common_parser], help="Analyze quota feasibility for a given duration.")

    _welcome()
    load_dotenv(ENV_PATH)
    check_crash_recovery()

    args = parser.parse_args()

    dur_min = 60.0
    if getattr(args, "minutes", None) is not None:
        dur_min = float(args.minutes)
    elif getattr(args, "hours", None) is not None:
        dur_min = float(args.hours) * 60.0

    if args.command == "estimate":
        print_lecture_estimate(
            duration_minutes=dur_min,
            audio_chunk_minutes=args.chunk_minutes,
            llm_chunks_per_block=args.chunks_per_block,
        )
        return

    is_clipboard = getattr(args, "clipboard", False)
    if not check_env(require_notion=not is_clipboard):
        sys.exit(1)

    if args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "resume":
        cmd_resume(args)
    elif args.command == "transcribe-only":
        cmd_transcribe_only(args)
    elif args.command == "generate-only":
        cmd_generate_only(args)


if __name__ == "__main__":
    main()
