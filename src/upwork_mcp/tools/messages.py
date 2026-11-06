"""Messaging tools for Upwork MCP."""

import asyncio
import re

from pydantic import BaseModel, Field
from ..browser.client import get_browser


class MessagesParams(BaseModel):
    """Parameters for getting messages."""
    room_id: str | None = Field(default=None, description="Specific chat room ID or URL")
    unread_only: bool = Field(default=False, description="Only show unread messages")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum conversations to return")


class SendMessageParams(BaseModel):
    """Parameters for sending a message."""
    room_id: str = Field(description="Chat room ID or URL")
    message: str = Field(description="Message content to send")


async def get_messages(params: MessagesParams) -> list[dict]:
    """Get messages from Upwork inbox.

    Returns a list of conversations with last message, sender info, and unread status.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    # The inbox lives under /ab/messages/rooms (/nx/messages is a 404).
    await page.goto("https://www.upwork.com/ab/messages/rooms",
                    wait_until="domcontentloaded")

    conversations = []

    # Wait for the room items themselves — the rooms panel renders before
    # its list hydrates, so waiting on the panel races an empty list.
    try:
        await page.wait_for_selector('[data-test="room-list-item"]', timeout=20000)
    except Exception:
        pass
    await asyncio.sleep(2)

    # Extract conversation items
    room_els = await page.query_selector_all('[data-test="room-list-item"]')

    for el in room_els[:params.limit]:
        try:
            conv = await _extract_conversation(el)
            if conv and (not params.unread_only or conv.get("unread")):
                conversations.append(conv)
        except Exception:
            continue

    return conversations


async def _extract_conversation(el) -> dict | None:
    """Extract conversation data from a room-list-item element."""
    conv = {}

    # Contact name
    name_el = await el.query_selector('[data-test="room-name"]')
    if name_el:
        conv["contact_name"] = (await name_el.text_content() or "").strip()

    if not conv.get("contact_name"):
        return None

    # Room URL/ID — the room link may be the item's ancestor or a child
    href = await el.evaluate(
        """e => {
            const a = e.closest('a[href*="/messages/rooms/"]')
                   || e.querySelector('a[href*="/messages/rooms/"]');
            return a ? a.getAttribute('href') : null;
        }"""
    )
    if href:
        conv["room_url"] = href if href.startswith("http") else f"https://www.upwork.com{href}"
        m = re.search(r"/rooms/([^/?]+)", href)
        if m:
            conv["room_id"] = m.group(1)

    # Related job / room topic
    topic_el = await el.query_selector('[data-test="room-topic-or-subtitle"]')
    if topic_el:
        conv["related_job"] = (await topic_el.text_content() or "").strip()

    # Timestamp
    time_el = await el.query_selector('time, .time')
    if time_el:
        conv["timestamp"] = (await time_el.text_content() or "").strip()

    # Last message preview: the item text minus the contact-name line
    raw = (await el.inner_text() or "").strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    preview = [ln for ln in lines
               if ln != conv["contact_name"]
               and ln != conv.get("timestamp")
               and ln != conv.get("related_job")]
    if preview:
        conv["last_message"] = " ".join(preview)[:300]

    # Unread indicator (badge or bolded room)
    unread_el = await el.query_selector(
        '[data-test="unread"], .unread-badge, .unread-indicator, [class*="unread"]')
    conv["unread"] = unread_el is not None

    return conv


async def get_conversation_messages(room_id: str, limit: int = 50) -> dict:
    """Get all messages in a specific conversation.

    Args:
        room_id: The room ID or URL
        limit: Maximum messages to return

    Returns conversation details with full message history.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    # Build URL
    if room_id.startswith("http"):
        url = room_id
    else:
        url = f"https://www.upwork.com/ab/messages/rooms/{room_id}"

    # The messages SPA keeps background requests alive, so networkidle can
    # hang indefinitely. Wait for the document, then give the room time to
    # hydrate its message history.
    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(5)

    conversation = {"room_id": room_id, "messages": []}

    # Contact name
    contact_el = await page.query_selector('[data-test="contact-name"], .contact-name, h2')
    if contact_el:
        conversation["contact_name"] = (await contact_el.text_content() or "").strip()

    # Related job
    job_el = await page.query_selector('[data-test="related-job"], .job-link')
    if job_el:
        conversation["related_job"] = (await job_el.text_content() or "").strip()

    # Extract messages
    message_els = await page.query_selector_all('[data-test="message"], .message-item, .chat-message')

    for el in message_els[-limit:]:  # Get last N messages
        try:
            msg = await _extract_message(el)
            if msg:
                conversation["messages"].append(msg)
        except Exception:
            continue

    # Upwork's current messages UI renders chat bubbles without the legacy
    # data-test hooks above. Preserve the visible transcript as a read-only
    # fallback so callers can still prepare for interviews and client calls.
    if not conversation["messages"]:
        body_text = (await page.locator("body").inner_text()).strip()
        if body_text:
            conversation["transcript_text"] = body_text[-30000:]

    return conversation


async def _extract_message(el) -> dict | None:
    """Extract message data from element."""
    msg = {}

    # Sender
    sender_el = await el.query_selector('[data-test="sender"], .sender, .author')
    if sender_el:
        msg["sender"] = (await sender_el.text_content() or "").strip()

    # Message content
    content_el = await el.query_selector('[data-test="content"], .content, .message-text, p')
    if content_el:
        msg["content"] = (await content_el.text_content() or "").strip()

    if not msg.get("content"):
        return None

    # Timestamp
    time_el = await el.query_selector('[data-test="timestamp"], time, .time')
    if time_el:
        msg["timestamp"] = (await time_el.text_content() or "").strip()

    # Check if it's from me
    me_indicator = await el.query_selector('.my-message, [data-test="my-message"], .sent')
    msg["is_mine"] = me_indicator is not None

    # Attachments
    attachment_els = await el.query_selector_all('[data-test="attachment"], .attachment')
    attachments = []
    for att in attachment_els:
        att_name = await att.text_content()
        if att_name:
            attachments.append(att_name.strip())
    if attachments:
        msg["attachments"] = attachments

    return msg


async def send_message(params: SendMessageParams) -> dict:
    """Send a message in a conversation.

    Args:
        params.room_id: Chat room ID or URL
        params.message: Message content

    Returns send status.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    # Navigate to conversation
    if params.room_id.startswith("http"):
        url = params.room_id
    else:
        url = f"https://www.upwork.com/nx/messages/{params.room_id}"

    await page.goto(url, wait_until="networkidle")

    # Find message input
    input_el = await page.query_selector('[data-test="message-input"], textarea[name*="message"], .message-input textarea')
    if not input_el:
        return {"status": "error", "message": "Message input not found"}

    # Type message
    await input_el.fill(params.message)

    # Find and click send button
    send_btn = await page.query_selector('[data-test="send-button"], button[type="submit"]:has-text("Send"), button:has-text("Send")')
    if not send_btn:
        # Try pressing Enter
        await input_el.press("Enter")
    else:
        await send_btn.click()

    # Wait for message to appear
    import asyncio
    await asyncio.sleep(2)

    # Verify message was sent by checking if input is cleared
    input_value = await input_el.input_value()
    if not input_value:
        return {"status": "sent", "message": "Message sent successfully"}

    return {"status": "unknown", "message": "Could not confirm message was sent"}


async def get_unread_count() -> dict:
    """Get count of unread messages.

    Returns total unread message count.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    # Check messages badge in header
    await page.goto("https://www.upwork.com/nx/find-work/", wait_until="networkidle")

    unread_el = await page.query_selector('[data-test="messages-badge"], .messages-count, .unread-count')
    if unread_el:
        text = (await unread_el.text_content() or "").strip()
        import re
        numbers = re.findall(r'\d+', text)
        if numbers:
            return {"unread_count": int(numbers[0])}

    return {"unread_count": 0}
