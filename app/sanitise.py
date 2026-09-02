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

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
ALLOWED = {"p", "br", "ul", "ol", "li", "strong", "em", "b", "i", "a",
           "table", "thead", "tbody", "tfoot", "tr", "th", "td"} | HEADINGS
DROP_WITH_CONTENT = {"script", "style", "iframe", "noscript", "svg",
                     "head", "title", "form", "select", "option", "button"}
VOID = {"br"}

# Applied to scraped copy only. A supplier page's h1 is the page title,
# and the shop page already has one of those, so it becomes an h2. The
# other levels stay as they are, and nothing typed in the editor is
# remapped.
SCRAPE_REMAP = {"h1": "h2"}

# Block containers that are not kept. Their direct text is wrapped in a
# paragraph, otherwise consecutive divs would run together as one line.
BLOCK_AS_P = {"div", "section", "article", "aside", "header", "footer",
              "main", "figure", "figcaption", "blockquote", "dl", "dt",
              "dd", "pre", "address", "details", "summary", "nav"}

# Tags whose direct text already sits inside a block, so no wrapper is
# needed. Starting one of the other block tags closes a synthetic wrapper.
TEXT_BLOCKS = {"p", "li", "td", "th"} | HEADINGS
STRUCTURE_BLOCKS = {"p", "ul", "ol", "table", "thead", "tbody", "tfoot",
                    "tr", "li", "td", "th"} | HEADINGS


class _Frame:
    __slots__ = ("tag", "out", "pending", "synthetic")

    def __init__(self, tag: str, out: str | None, pending: bool = False):
        self.tag = tag          # tag as it appeared in the source
        self.out = out          # tag that was emitted, if any
        self.pending = pending  # may still need a synthetic <p>
        self.synthetic = False  # a synthetic <p> is open for this frame


class _Cleaner(HTMLParser):
    def __init__(self, remap: dict[str, str] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.frames: list[_Frame] = []
        self.remap = remap or {}

    # -- helpers -----------------------------------------------------------

    def _in_text_block(self) -> bool:
        return any(f.out in TEXT_BLOCKS or f.synthetic for f in self.frames)

    def _open_synthetic_p(self) -> None:
        """Text is about to appear directly inside a dropped container."""
        if self._in_text_block():
            return
        for frame in reversed(self.frames):
            if frame.pending:
                frame.pending = False
                frame.synthetic = True
                self.out.append("<p>")
                return

    def _close_synthetic_p(self) -> None:
        for frame in reversed(self.frames):
            if frame.synthetic:
                frame.synthetic = False
                self.out.append("</p>")
                return
            if frame.out in TEXT_BLOCKS:
                return

    def _close_open_paragraph(self) -> None:
        """A block cannot live inside a paragraph. Browsers produce
        <p><ol>...</ol></p> from a list command; close the paragraph
        first so the list stands on its own."""
        for index in range(len(self.frames) - 1, -1, -1):
            frame = self.frames[index]
            if frame.out == "p":
                while len(self.frames) > index:
                    self._close_frame(self.frames.pop())
                return
            if frame.out in TEXT_BLOCKS or frame.synthetic:
                return

    def _close_frame(self, frame: _Frame) -> None:
        if frame.synthetic:
            self.out.append("</p>")
        if frame.out:
            self.out.append(f"</{frame.out}>")

    # -- parser callbacks --------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in DROP_WITH_CONTENT:
            self.skip += 1
            return
        if self.skip:
            return
        out = self.remap.get(tag, tag)

        if out in VOID:
            self._open_synthetic_p()
            self.out.append(f"<{out}>")
            return

        if out in ALLOWED:
            if out in STRUCTURE_BLOCKS:
                self._close_synthetic_p()
                if out not in ("li", "td", "th", "tr", "thead", "tbody",
                               "tfoot"):
                    self._close_open_paragraph()
            else:
                self._open_synthetic_p()
            if out == "a":
                href = dict(attrs).get("href") or ""
                if not href.startswith(("http://", "https://")):
                    # Unwrap: the text stays, the link goes.
                    self.frames.append(_Frame(tag, None))
                    return
                self.out.append(f'<a href="{escape(href, quote=True)}">')
            else:
                self.out.append(f"<{out}>")
            self.frames.append(_Frame(tag, out))
            return

        if tag in BLOCK_AS_P:
            self.frames.append(_Frame(tag, None, pending=True))
            return

        # Unknown inline tag (span, font, ...): keep the text only.
        self.frames.append(_Frame(tag, None))

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if not self.skip and tag in VOID:
            self._open_synthetic_p()
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag in VOID:
            return
        if not any(f.tag == tag for f in self.frames):
            return
        # Close intervening frames too, so nesting mistakes cannot leave
        # anything dangling open.
        while self.frames:
            frame = self.frames.pop()
            self._close_frame(frame)
            if frame.tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.skip or not data:
            return
        if data.strip():
            self._open_synthetic_p()
        self.out.append(escape(data))


def sanitise_html(html: str, remap: dict[str, str] | None = None) -> str:
    """Clean HTML down to the allowed tags. `remap` renames tags on the
    way through; pass SCRAPE_REMAP for copy that came from a supplier
    page and nothing for copy the user wrote."""
    cleaner = _Cleaner(remap)
    cleaner.feed(html or "")
    cleaner.close()
    while cleaner.frames:
        cleaner._close_frame(cleaner.frames.pop())
    result = "".join(cleaner.out)
    # Whitespace next to a block tag is layout, not content. Whitespace
    # between inline tags is a real space and stays.
    blocks = "p|ul|ol|li|h[1-6]|table|thead|tbody|tfoot|tr|td|th"
    result = re.sub(rf"(</?(?:{blocks})>)\s+", r"\1", result)
    result = re.sub(rf"\s+(</?(?:{blocks})>)", r"\1", result)
    # Empty blocks, including the <p><br></p> a contenteditable leaves
    # behind when a line is started and abandoned.
    result = re.sub(r"<(p|li|h[1-6]|td|th)>(?:\s|<br>)*</\1>", "", result)
    # A cell or table whose contents were all empty
    result = re.sub(r"<(tr|thead|tbody|tfoot)>\s*</\1>", "", result)
    result = re.sub(r"<table>\s*</table>", "", result)
    return result.strip()


def text_only(html: str) -> str:
    """Plain text for the LLM: no point paying for markup tokens."""
    tree = Selectolax(html or "")
    for selector in ("script", "style"):
        for node in tree.css(selector):
            node.decompose()
    text = tree.text(separator="\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()
