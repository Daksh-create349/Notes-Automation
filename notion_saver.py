import os
from notion_client import Client


def _md_to_blocks(markdown: str) -> list:
    blocks = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1",
                "heading_1": {"rich_text": [{"type": "text", "text": {"content": stripped[2:]}}]}})
        elif stripped.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": stripped[3:]}}]}})
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": stripped[2:]}}]}})
        else:
            blocks.append({"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": stripped}}]}})
    return blocks


def save_notes_to_notion(title: str, markdown_content: str) -> str | None:
    try:
        client = Client(auth=os.environ["NOTION_TOKEN"])
        # Strip any hyphens — the API accepts both formats but we normalise here
        parent_id = os.environ["NOTION_PAGE_ID"].replace("-", "")

        page = client.pages.create(
            parent={"type": "page_id", "page_id": parent_id},
            properties={"title": {"title": [{"type": "text", "text": {"content": title}}]}},
            children=_md_to_blocks(markdown_content),
        )

        url = page.get("url", f"https://notion.so/{page['id'].replace('-', '')}")
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

