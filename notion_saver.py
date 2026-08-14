import os
from notion_client import Client


def _md_to_blocks(markdown: str) -> list:
    """Convert Markdown string into Notion API block objects."""
    blocks = []
    lines = markdown.splitlines()
    in_code_block = False
    code_lines = []
    code_lang = "plain text"

    for line in lines:
        stripped = line.strip()

        # Handle Code Blocks
        if stripped.startswith("```"):
            if in_code_block:
                # End of code block
                code_content = "\n".join(code_lines)
                if code_content:
                    blocks.append({
                        "object": "block",
                        "type": "code",
                        "code": {
                            "rich_text": [{"type": "text", "text": {"content": code_content[:2000]}}],
                            "language": code_lang if code_lang else "plain text"
                        }
                    })
                in_code_block = False
                code_lines = []
                code_lang = "plain text"
            else:
                # Start of code block
                in_code_block = True
                lang = stripped[3:].strip()
                code_lang = lang if lang else "javascript"
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not stripped:
            continue

        # Headings
        if stripped.startswith("### "):
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": stripped[4:]}}]}
            })
        elif stripped.startswith("## "):
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": stripped[3:]}}]}
            })
        elif stripped.startswith("# "):
            blocks.append({
                "object": "block",
                "type": "heading_1",
                "heading_1": {"rich_text": [{"type": "text", "text": {"content": stripped[2:]}}]}
            })
        # Blockquotes / Callouts
        elif stripped.startswith("> "):
            blocks.append({
                "object": "block",
                "type": "quote",
                "quote": {"rich_text": [{"type": "text", "text": {"content": stripped[2:]}}]}
            })
        # Dividers
        elif stripped in ("---", "***", "___"):
            blocks.append({
                "object": "block",
                "type": "divider",
                "divider": {}
            })
        # Bullet Lists
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": stripped[2:]}}]}
            })
        # Numbered Lists
        elif len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in (".", ")"):
            idx = 2 if stripped[1] in (".", ")") else 3
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": [{"type": "text", "text": {"content": stripped[idx:].strip()}}]}
            })
        # Regular Paragraph
        else:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": stripped[:2000]}}]}
            })

    return blocks


def save_notes_to_notion(title: str, markdown_content: str) -> str | None:
    try:
        client = Client(auth=os.environ["NOTION_TOKEN"])
        parent_id = os.environ["NOTION_PAGE_ID"].replace("-", "")

        all_blocks = _md_to_blocks(markdown_content)
        first_batch = all_blocks[:100]

        # Create page with first batch of blocks (max 100 blocks)
        page = client.pages.create(
            parent={"type": "page_id", "page_id": parent_id},
            properties={"title": {"title": [{"type": "text", "text": {"content": title[:2000]}}]}},
            children=first_batch,
        )

        page_id = page["id"]

        # Append remaining blocks in batches of 100 if notes are very long
        remaining_blocks = all_blocks[100:]
        while remaining_blocks:
            batch = remaining_blocks[:100]
            client.blocks.children.append(block_id=page_id, children=batch)
            remaining_blocks = remaining_blocks[100:]

        url = page.get("url", f"https://notion.so/{page_id.replace('-', '')}")
        print(f"Saved to Notion: {url}")
        return url
    except Exception as e:
        error_msg = str(e)
        if "Could not find page" in error_msg or "ObjectNotFound" in error_msg:
            print(
                "\n❌ Notion Error: Page not found.\n"
                "   Fix: Open your Notion page → click '...' (top right)\n"
                "        → Connections → Connect to → select 'Notes CSE SEM 3'\n"
                "   You must share the page with your integration before it can save there.\n"
            )
        else:
            print(f"Notion save error: {e}")
        return None
