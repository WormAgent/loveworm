"""Read-only Instagram DMs by reading the instagram.com pages in the bot's browser.

The web no longer serves DMs through its REST api, so this opens each profile, clicks its
message button and reads the conversation on screen. It never types or sends anything.
"""
import re
import time

from selenium.webdriver.common.by import By

# seconds between page actions, to keep a human pace
PAUSE = 5

# "@sofi.perez" (the lookbehind skips emails) or "ig: sofi" / "insta: sofi"
_AT_HANDLE = re.compile(r"(?<![\w.])@([a-z0-9._]{3,30})", re.IGNORECASE)
_LABELED_HANDLE = re.compile(r"\b(?:ig|insta|instagram)\s*(?::|=|->)\s*([a-z0-9._]{3,30})", re.IGNORECASE)
# "mi ig es sofi.perez" or a message that is only the handle: needs a dot, underscore or digit,
# so plain words ("ig es mejor") don't count
_SPOKEN_HANDLE = re.compile(r"\b(?:ig|insta|instagram)\s+es\s+([a-z0-9._]{3,30})", re.IGNORECASE)
_BARE_HANDLE = re.compile(r"^\s*([a-z0-9._]{3,30})\s*$", re.IGNORECASE)

_MESSAGE_BUTTON = ("//*[(self::button or @role='button') and "
                   "(normalize-space(.)='Message' or normalize-space(.)='Enviar mensaje' or normalize-space(.)='Mensaje')]")
_NOT_NOW = "//button[normalize-space(.)='Not Now' or normalize-space(.)='Not now' or normalize-space(.)='Ahora no']"

# reads the visible conversation: one entry per row with text, side decided by where the bubble sits
_READ_JS = """
const rows = [...document.querySelectorAll('div[role="row"]')];
const out = [];
for (const row of rows) {
  const parts = [...row.querySelectorAll('div[dir="auto"], span[dir="auto"]')]
    .filter(el => !el.parentElement.closest('[dir="auto"]'));
  const text = parts.map(el => el.innerText.trim()).filter(Boolean).join(' ');
  if (!text) continue;
  const r = row.getBoundingClientRect();
  const b = parts[0].getBoundingClientRect();
  out.push({text, from_me: (b.left + b.right) / 2 > (r.left + r.right) / 2});
}
return {rows: rows.length, messages: out};
"""

# scrolls the conversation to the top so instagram loads older messages
_SCROLL_UP_JS = """
const row = document.querySelector('div[role="row"]');
let el = row && row.parentElement;
while (el && el.scrollHeight <= el.clientHeight + 5) el = el.parentElement;
if (!el) return false;
el.scrollTop = 0;
return true;
"""


def _looks_like_handle(h):
    return any(c in h for c in "._") or any(c.isdigit() for c in h)


def extract_handles(texts):
    """texts: the other person's messages. Returns the instagram handles they shared, in order."""
    handles = []
    for text in texts:
        text = text or ""
        found = _AT_HANDLE.findall(text) + _LABELED_HANDLE.findall(text)
        found += [h for h in _SPOKEN_HANDLE.findall(text) + _BARE_HANDLE.findall(text) if _looks_like_handle(h)]
        for h in found:
            h = h.strip(".").lower()
            if len(h) >= 3 and h not in handles:
                handles.append(h)
    return handles


class InstagramWeb:

    def __init__(self, browser):
        self.browser = browser

    def _ensure_instagram(self):
        if "instagram.com" not in self.browser.current_url:
            self.browser.get("https://www.instagram.com/")
            time.sleep(PAUSE)

    def is_logged_in(self):
        self._ensure_instagram()
        # ds_user_id only exists with a session
        return "ds_user_id=" in self.browser.execute_script("return document.cookie")

    def _dismiss_popups(self):
        for button in self.browser.find_elements(By.XPATH, _NOT_NOW):
            try:
                button.click()
                time.sleep(1)
            except Exception:
                pass

    def read_chat(self, username, scrolls=2):
        """Returns ([{"from_me", "text", "sent"}] oldest first, note).
        messages is None when the chat could not be opened, and [] when it opened empty."""
        self.browser.get(f"https://www.instagram.com/{username}/")
        time.sleep(PAUSE)
        self._dismiss_popups()

        buttons = self.browser.find_elements(By.XPATH, _MESSAGE_BUTTON)
        if not buttons:
            return None, "sin botón de mensaje (perfil inexistente, privado o es el tuyo)"
        buttons[0].click()
        time.sleep(PAUSE)
        self._dismiss_popups()

        if "/direct/" not in self.browser.current_url:
            return None, f"no abrió el chat ({self.browser.current_url})"

        # older messages load when the conversation is scrolled to the top
        for _ in range(scrolls):
            if not self.browser.execute_script(_SCROLL_UP_JS):
                break
            time.sleep(PAUSE)

        result = self.browser.execute_script(_READ_JS)
        messages = [{"from_me": m["from_me"], "text": m["text"], "sent": None} for m in result["messages"]]
        return messages, f"{result['rows']} filas, {len(messages)} mensajes"
