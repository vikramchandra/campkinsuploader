"""Cleaning HTML that came from a contenteditable paste or a scraped page.

Pasted manufacturer copy arrives full of span soup, inline styles and
tracking pixels. Only a small whitelist of structural tags survives; the
same rule is applied client-side on paste, and here again before anything
is sent to WooCommerce or the LLM, since the client can be bypassed.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

from selectolax.parser import HTMLParser as Selectolax

ALLOWED = {"p", "br", "ul", "ol", "li", "strong", "em", "b", "i",
           "h2", "h3", "h4", "a"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "noscript", "svg",
                     "head", "title", "form", "select", "option", "button"}
VOID = {"br"}


class _Cleaner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in DROP_WITH_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED:
            return
        if tag in VOID:
            self.out.append(f"<{tag}>")
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if not href.startswith(("http://", "https://")):
                return  # Unwrap: the text stays, the link goes.
            self.out.append(f'<a href="{escape(href, quote=True)}">')
        else:
            self.out.append(f"<{tag}>")
        self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if not self.skip and tag in VOID:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in ALLOWED or tag in VOID:
            return
        if tag in self.open_tags:
            # Close intervening tags too, so nesting mistakes cannot leave
            # anything dangling open.
            while self.open_tags:
                open_tag = self.open_tags.pop()
                self.out.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data: str) -> None:
        if not self.skip and data:
            self.out.append(escape(data))


def sanitise_html(html: str) -> str:
    cleaner = _Cleaner()
    cleaner.feed(html or "")
    cleaner.close()
    while cleaner.open_tags:
        cleaner.out.append(f"</{cleaner.open_tags.pop()}>")
    result = "".join(cleaner.out)
    result = re.sub(r"<(p|li|h2|h3|h4)>\s*</\1>", "", result)
    return result.strip()


def text_only(html: str) -> str:
    """Plain text for the LLM: no point paying for markup tokens."""
    tree = Selectolax(html or "")
    for selector in ("script", "style"):
        for node in tree.css(selector):
            node.decompose()
    text = tree.text(separator="\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()
