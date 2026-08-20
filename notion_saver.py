import json
import os
import re
import time
from typing import cast
from notion_client import Client

# --------------------------------------------------------------------------- #
# Notion Language Support & Normalization
# --------------------------------------------------------------------------- #
NOTION_LANGUAGES = {
    "abap", "arduino", "bash", "basic", "c", "clojure", "coffeescript", "c++", "c#",
    "css", "dart", "diff", "docker", "elixir", "elm", "erlang", "flow", "fortran",
    "f#", "gherkin", "glsl", "go", "graphql", "groovy", "haskell", "html", "java",
    "javascript", "json", "julia", "kotlin", "latex", "less", "lisp", "livescript",
    "lua", "makefile", "markdown", "markup", "matlab", "mermaid", "nix", "objective-c",
    "ocaml", "pascal", "perl", "php", "plain text", "powershell", "prolog", "protobuf",
    "python", "r", "reason", "ruby", "rust", "sass", "scala", "scheme", "scss",
    "shell", "sql", "swift", "typescript", "vb.net", "verilog", "vhdl", "visual basic",
    "webassembly", "xml", "yaml", "java/c/c++/c#"
}

LANGUAGE_ALIASES = {
    "py": "python",
    "python3": "python",
    "sh": "bash",
    "zsh": "bash",
    "shell": "shell",
    "js": "javascript",
    "node": "javascript",
    "ts": "typescript",
    "cpp": "c++",
    "cxx": "c++",
    "cs": "c#",
    "csharp": "c#",
    "dockerfile": "docker",
    "yml": "yaml",
    "md": "markdown",
    "rs": "rust",
    "rb": "ruby",
    "ps1": "powershell",
    "txt": "plain text",
    "text": "plain text",
}

VALID_NOTION_COLORS = {
    "default", "gray", "brown", "orange", "yellow", "green", "blue", "purple", "pink", "red",
    "gray_background", "brown_background", "orange_background", "yellow_background",
    "green_background", "blue_background", "purple_background", "pink_background", "red_background"
}

EMOJI_SAFE_MAP = {
    "→": "➡️", "->": "➡️", "▪": "📌", "?": "❓", "!": "❗",
    "⚠": "⚠️", "✓": "✅", "x": "❌", "*": "⭐", "i": "ℹ️",
}


def safe_emoji(icon_tag: str | None, default: str = "📌") -> str:
    """Normalize and validate a Notion emoji string to ensure valid unicode emoji."""
    if not icon_tag:
        return default
    s = icon_tag.strip()
    if s in EMOJI_SAFE_MAP:
        return EMOJI_SAFE_MAP[s]
    if len(s) <= 2:
        return s
    return default


def normalize_color(color_tag: str | None, default: str = "default") -> str:
    """Normalize and validate a Notion color string."""
    if not color_tag:
        return default
    c = color_tag.strip().lower()
    if c in VALID_NOTION_COLORS:
        return c
    return default


def normalize_language(lang_tag: str) -> str:
    """Normalize markdown language tag to a Notion-supported code block language."""
    tag = lang_tag.strip().lower()
    if not tag:
        return "plain text"
    tag = LANGUAGE_ALIASES.get(tag, tag)
    if tag in NOTION_LANGUAGES:
        return tag
    return "plain text"


def sanitize_error(err: Exception | str) -> str:
    """Scrub sensitive API tokens or keys from error strings before logging."""
    msg = str(err)
    msg = re.sub(r"gsk_[A-Za-z0-9_\-]+", "gsk_***[REDACTED]***", msg)
    msg = re.sub(r"ntn_[A-Za-z0-9_\-]+", "ntn_***[REDACTED]***", msg)
    msg = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer ***[REDACTED]***", msg)
    return msg


def _text_to_rich_text(content: str) -> list[dict]:
    """Convert text to Notion rich_text objects with full inline markdown support.
    
    Parses **bold**, *italic*, `code`, ~~strikethrough~~, and [links](url),
    stripping broken markdown artifacts like **** or ** **.
    """
    if not content:
        return []

    # Clean up empty or broken markdown artifacts like ****, ** **, __ __
    cleaned = re.sub(r"\*{4,}", "", content)
    cleaned = re.sub(r"\*\*\s*\*\*", "", cleaned)
    cleaned = re.sub(r"__\s*__", "", cleaned)

    pattern = re.compile(
        r"(?P<bold_italic>\*\*\*(.+?)\*\*\*|___(.+?)___)|"
        r"(?P<bold>\*\*(.+?)\*\*|__(.+?)__)|"
        r"(?P<italic>\*(.+?)\*|_(.+?)_)|"
        r"(?P<code>`(.+?)`)|"
        r"(?P<strike>~~(.+?)~~)|"
        r"(?P<link>\[(.+?)\]\(((?:https?://|/).+?)\))"
    )

    rich_text = []
    last_idx = 0

    for match in pattern.finditer(cleaned):
        start, end = match.span()
        if start > last_idx:
            plain = cleaned[last_idx:start]
            if plain:
                for chunk in [plain[i:i + 2000] for i in range(0, len(plain), 2000)]:
                    rich_text.append({"type": "text", "text": {"content": chunk}})

        gd = match.groupdict()
        if gd.get("bold_italic"):
            txt = match.group(2) or match.group(3) or ""
            rich_text.append({"type": "text", "text": {"content": txt}, "annotations": {"bold": True, "italic": True}})
        elif gd.get("bold"):
            txt = match.group(5) or match.group(6) or ""
            rich_text.append({"type": "text", "text": {"content": txt}, "annotations": {"bold": True}})
        elif gd.get("italic"):
            txt = match.group(8) or match.group(9) or ""
            rich_text.append({"type": "text", "text": {"content": txt}, "annotations": {"italic": True}})
        elif gd.get("code"):
            txt = match.group(11) or ""
            rich_text.append({"type": "text", "text": {"content": txt}, "annotations": {"code": True}})
        elif gd.get("strike"):
            txt = match.group(13) or ""
            rich_text.append({"type": "text", "text": {"content": txt}, "annotations": {"strikethrough": True}})
        elif gd.get("link"):
            link_text = match.group(15) or ""
            link_url = match.group(16) or ""
            rich_text.append({"type": "text", "text": {"content": link_text, "link": {"url": link_url}}})

        last_idx = end

    if last_idx < len(cleaned):
        plain = cleaned[last_idx:]
        if plain:
            for chunk in [plain[i:i + 2000] for i in range(0, len(plain), 2000)]:
                rich_text.append({"type": "text", "text": {"content": chunk}})

    return rich_text


def block_to_notion(item: dict) -> dict | None:
    """Convert a single structured JSON block into a Notion API block dictionary."""
    if not isinstance(item, dict):
        return None

    b_type = item.get("type", "")
    text = str(item.get("text", ""))
    color = normalize_color(item.get("color"))

    if b_type in ("heading_1", "heading_2", "heading_3"):
        return {
            "object": "block",
            "type": b_type,
            b_type: {
                "rich_text": _text_to_rich_text(text),
                "color": color,
            }
        }
    elif b_type == "paragraph":
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": _text_to_rich_text(text),
                "color": color,
            }
        }
    elif b_type in ("bulleted_list_item", "numbered_list_item"):
        return {
            "object": "block",
            "type": b_type,
            b_type: {
                "rich_text": _text_to_rich_text(text),
                "color": color,
            }
        }
    elif b_type == "callout":
        callout_color = normalize_color(item.get("color"), default="blue_background")
        icon_symbol = safe_emoji(item.get("icon"), default="📌")
        return {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": _text_to_rich_text(text),
                "icon": {"type": "emoji", "emoji": icon_symbol},
                "color": callout_color,
            }
        }
    elif b_type == "divider":
        return {
            "object": "block",
            "type": "divider",
            "divider": {}
        }
    elif b_type == "toggle":
        children_data = item.get("children", [])
        children_blocks = []
        if children_data:
            for child in children_data:
                converted = block_to_notion(child)
                if converted:
                    children_blocks.append(converted)
        
        toggle_payload: dict = {
            "rich_text": _text_to_rich_text(text),
            "color": color,
        }
        if children_blocks:
            toggle_payload["children"] = children_blocks
            
        return {
            "object": "block",
            "type": "toggle",
            "toggle": toggle_payload
        }
    elif b_type == "quote":
        return {
            "object": "block",
            "type": "quote",
            "quote": {
                "rich_text": _text_to_rich_text(text),
                "color": color,
            }
        }
    elif b_type == "code":
        lang = normalize_language(item.get("language", "plain text"))
        return {
            "object": "block",
            "type": "code",
            "code": {
                "rich_text": [{"type": "text", "text": {"content": text}}],
                "language": lang,
            }
        }
    elif b_type == "table":
        rows = item.get("rows", [])
        if rows:
            width = max(len(r) for r in rows)
            normalized_rows = [r + [""] * (width - len(r)) for r in rows]
            return {
                "object": "block",
                "type": "table",
                "table": {
                    "table_width": width,
                    "has_column_header": True,
                    "has_row_header": False,
                    "children": [
                        {
                            "type": "table_row",
                            "table_row": {
                                "cells": [_text_to_rich_text(str(cell)) for cell in r]
                            }
                        }
                        for r in normalized_rows
                    ]
                }
            }
    return None


def json_to_notion_blocks(data: dict) -> list[dict]:
    """Convert a dictionary containing a list of JSON blocks directly into Notion API blocks."""
    blocks = []
    items = data.get("blocks", []) if isinstance(data, dict) else []
    for item in items:
        converted = block_to_notion(item)
        if converted:
            blocks.append(converted)
    return blocks


def _md_to_blocks(markdown: str) -> list[dict]:
    """Convert Markdown string into aesthetically structured Notion API block objects."""
    blocks = []
    lines = markdown.splitlines()
    in_code_block = False
    code_lang = "plain text"
    code_lines = []
    table_lines = []
    h1_colors = ["blue", "purple", "green"]
    h1_idx = 0

    def flush_table():
        nonlocal table_lines
        if not table_lines:
            return
        rows = []
        for t_line in table_lines:
            stripped_t = t_line.strip()
            clean_sep = re.sub(r"[\s\-:|]", "", stripped_t)
            if not clean_sep:
                continue
            cells = [c.strip() for c in stripped_t.split("|")[1:-1]]
            if cells:
                rows.append(cells)
        if rows:
            width = max(len(r) for r in rows)
            normalized_rows = [r + [""] * (width - len(r)) for r in rows]
            blocks.append({
                "object": "block",
                "type": "table",
                "table": {
                    "table_width": width,
                    "has_column_header": True,
                    "has_row_header": False,
                    "children": [
                        {
                            "type": "table_row",
                            "table_row": {
                                "cells": [_text_to_rich_text(cell) for cell in r]
                            }
                        }
                        for r in normalized_rows
                    ]
                }
            })
        table_lines = []

    for line in lines:
        stripped = line.strip()

        # Handle table rows
        if stripped.startswith("|") and stripped.endswith("|"):
            table_lines.append(stripped)
            continue
        elif table_lines:
            flush_table()

        # Toggle code block state
        if stripped.startswith("```"):
            if in_code_block:
                code_content = "\n".join(code_lines)
                blocks.append({
                    "object": "block",
                    "type": "code",
                    "code": {
                        "rich_text": [{"type": "text", "text": {"content": code_content}}],
                        "language": code_lang,
                    }
                })
                code_lines = []
                in_code_block = False
                code_lang = "plain text"
            else:
                raw_lang = stripped[3:].strip()
                code_lang = normalize_language(raw_lang)
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not stripped:
            continue

        if stripped.startswith("---") or stripped.startswith("***"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif stripped.startswith("# "):
            color = h1_colors[h1_idx % len(h1_colors)]
            h1_idx += 1
            blocks.append({
                "object": "block",
                "type": "heading_1",
                "heading_1": {"rich_text": _text_to_rich_text(stripped[2:].strip()), "color": color}
            })
        elif stripped.startswith("## "):
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": _text_to_rich_text(stripped[3:].strip()), "color": "default"}
            })
        elif stripped.startswith("### "):
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": _text_to_rich_text(stripped[4:].strip()), "color": "default"}
            })
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _text_to_rich_text(stripped[2:].strip())}
            })
        elif re.match(r"^\d+\.\s+", stripped):
            item_text = re.sub(r"^\d+\.\s+", "", stripped)
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _text_to_rich_text(item_text)}
            })
        elif stripped.startswith("> "):
            quote_raw = stripped[2:].strip()

            # Beautiful callout parsing with smart icon & background colors
            if re.search(r"^\s*(\*\*Note:?\*\*|⚠️|\*\*Warning:?\*\*)", quote_raw, re.IGNORECASE):
                callout_text = re.sub(r"^\s*(\*\*Note:?\*\*|⚠️|\*\*Warning:?\*\*)\s*:?\s*", "", quote_raw).strip()
                blocks.append({
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": _text_to_rich_text(callout_text),
                        "icon": {"type": "emoji", "emoji": "⚠️"},
                        "color": "yellow_background",
                    }
                })
            elif re.search(r"^\s*(\*\*Key Takeaway:?\*\*|✅)", quote_raw, re.IGNORECASE):
                callout_text = re.sub(r"^\s*(\*\*Key Takeaway:?\*\*|✅)\s*:?\s*", "", quote_raw).strip()
                blocks.append({
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": _text_to_rich_text(callout_text),
                        "icon": {"type": "emoji", "emoji": "✅"},
                        "color": "green_background",
                    }
                })
            elif re.search(r"^\s*(\*\*Q&A:?\*\*|\*\*Q:\*\*|\?|❓)", quote_raw, re.IGNORECASE):
                callout_text = re.sub(r"^\s*(\*\*Q&A:?\*\*|\*\*Q:\*\*|\?|❓)\s*:?\s*", "", quote_raw).strip()
                blocks.append({
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": _text_to_rich_text(callout_text),
                        "icon": {"type": "emoji", "emoji": "❓"},
                        "color": "gray_background",
                    }
                })
            elif re.search(r"^\s*(\*\*Priority:?\*\*|🔥|❗)", quote_raw, re.IGNORECASE):
                callout_text = re.sub(r"^\s*(\*\*Priority:?\*\*|🔥|❗)\s*:?\s*", "", quote_raw).strip()
                blocks.append({
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": _text_to_rich_text(callout_text),
                        "icon": {"type": "emoji", "emoji": "🔥"},
                        "color": "orange_background",
                    }
                })
            elif re.search(r"^\s*(\*\*Overview:?\*\*|\*\*Preview:?\*\*|🎯|➡️)", quote_raw, re.IGNORECASE):
                callout_text = re.sub(r"^\s*(\*\*Overview:?\*\*|\*\*Preview:?\*\*|🎯|➡️)\s*:?\s*", "", quote_raw).strip()
                blocks.append({
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": _text_to_rich_text(callout_text),
                        "icon": {"type": "emoji", "emoji": "🎯"},
                        "color": "blue_background",
                    }
                })
            else:
                blocks.append({
                    "object": "block",
                    "type": "quote",
                    "quote": {"rich_text": _text_to_rich_text(quote_raw)}
                })
        else:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": _text_to_rich_text(stripped)}
            })

    if table_lines:
        flush_table()

    if in_code_block and code_lines:
        code_content = "\n".join(code_lines)
        blocks.append({
            "object": "block",
            "type": "code",
            "code": {
                "rich_text": [{"type": "text", "text": {"content": code_content}}],
                "language": code_lang,
            }
        })

    return blocks


def save_notes_to_notion(title: str, content: str | dict) -> str | None:
    """Save structured JSON or Markdown notes to a new Notion page with error-safe batching.
    
    Guarantees that a Notion page is NEVER created if content or blocks are empty.
    """
    if not content:
        print("  ⚠️ No content to save to Notion. Skipping Notion page creation.")
        return None
    if isinstance(content, str) and not content.strip():
        print("  ⚠️ Empty content provided to Notion saver. Skipping Notion page creation.")
        return None

    # Convert content to Notion blocks BEFORE creating the page
    if isinstance(content, dict):
        blocks = json_to_notion_blocks(content)
    elif isinstance(content, str) and content.strip().startswith("{") and '"blocks"' in content:
        try:
            parsed_json = json.loads(content)
            blocks = json_to_notion_blocks(parsed_json)
        except Exception:
            blocks = _md_to_blocks(content)
    else:
        blocks = _md_to_blocks(content)

    if not blocks:
        print("  ⚠️ No valid Notion blocks generated from content. Skipping Notion page creation.")
        return None

    clean_title = (title.strip() if (title and title.strip()) else "Lecture Notes")[:2000]

    notion_token = os.environ.get("NOTION_TOKEN")
    notion_page_id = os.environ.get("NOTION_PAGE_ID")
    if not notion_token or not notion_page_id:
        print("  ⚠️ Notion credentials (NOTION_TOKEN / NOTION_PAGE_ID) missing or empty. Falling back to clipboard.")
        return None

    try:
        client = Client(auth=notion_token)
        parent_id = notion_page_id.replace("-", "")

        print("  Creating Notion page...")
        page_resp = client.pages.create(
            parent={"type": "page_id", "page_id": parent_id},
            icon={"type": "emoji", "emoji": "📚"},
            properties={"title": {"title": [{"type": "text", "text": {"content": clean_title}}]}},
        )
        page = cast(dict, page_resp)

        print(f"  Uploading {len(blocks)} blocks in batches of 80...")
        batch_size = 80
        page_id = str(page.get("id", ""))
        for i in range(0, len(blocks), batch_size):
            batch = blocks[i:i + batch_size]
            batch_num = i // batch_size + 1
            uploaded = False
            for b_attempt in range(3):
                try:
                    client.blocks.children.append(block_id=page_id, children=batch)
                    uploaded = True
                    break
                except Exception as batch_err:
                    err_s = sanitize_error(batch_err)
                    if "429" in err_s or "rate" in err_s.lower():
                        time.sleep(2 * (b_attempt + 1))
                    elif b_attempt == 2:
                        print(f"  ⚠️ Warning: Notion rejected batch {batch_num} ({len(batch)} blocks): {err_s}")
                        # Fallback: append individual valid blocks
                        for single_b in batch:
                            try:
                                client.blocks.children.append(block_id=page_id, children=[single_b])
                            except Exception:
                                pass

        url = str(page.get("url", f"https://notion.so/{page_id.replace('-', '')}"))
        print(f"Saved to Notion: {url}")
        return url
    except Exception as e:
        error_msg = sanitize_error(e)
        if "Could not find page" in error_msg or "ObjectNotFound" in error_msg:
            print(
                "\n❌ Notion Error: Page not found.\n"
                "   Fix: Open your Notion page → click '...' (top right)\n"
                "        → Connections → Connect to → select your Notion integration\n"
                "   You must share the page with your integration before it can save there.\n"
            )
        else:
            print(f"Notion save error: {error_msg}")
        return None
