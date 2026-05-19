#!/usr/bin/env python3
"""Post-process make4ht HTML output to fix code-block rendering artifacts.

tex4ht's Verbatim renderer emits three kinds of garbage that hurt readability
in the browser:

  1. Indentation whitespace is parked inside the line-number <span> (class
     ecrm-0500), which CSS gives a fixed inline-block width. The spaces are
     swallowed, so every code line starts at the same column regardless of
     indent depth -- and for multi-digit line numbers, no separator is
     emitted at all, fusing the number into the code ("20MODULE_LICENSE").
  2. Each Verbatim line is padded out to a fixed character width with
     trailing spaces, which render as visible whitespace runs.
  3. Whitespace-only orphan lines appear mid-listing (page-break/overfull
     vbox leftovers from the PDF layout flow).

This script fixes all three. Run it on html/lkmpg-for-ht.html after make4ht.
"""

import re
import sys
from pathlib import Path


LINENO_SPAN_WITH_SPACES = re.compile(
    r"<span class='ecrm-0500'>(\d+)([ ]+)</span>"
)
LINENO_SPAN_BARE = re.compile(
    r"<span class='ecrm-0500'>(\d+)</span>(?=[^ \t\n])"
)
LINENO_SPAN_NEWLINE = re.compile(
    r"<span class='ecrm-0500'>(\d+)\n</span>"
)
FANCYVRB_BLOCK = re.compile(
    r"(<pre class='fancyvrb'[^>]*>)(.*?)(</pre>)",
    re.DOTALL,
)
# tex4ht sometimes inlines two source lines into one HTML line: a lineno
# span follows the previous </span> on the same line, either directly or
# separated by tex4ht's column-padding whitespace. The (?<!\n) guard
# avoids touching the common case where </span> already begins a new HTML
# line (legitimate per-line separator).
MERGED_LINES = re.compile(
    r"(?<!\n)</span>[ \t]*(<span class='ecrm-0500'>)"
)
# Trailing whitespace inside a code span (the per-line column padding).
# [^<]* keeps us from greedily crossing into the next span.
CODE_SPAN_TRAILING_WS = re.compile(
    r"(<span class='ectt-\d+'>[^<]*?)[ \t]+</span>"
)

# Leading whitespace inside a code span (used to detect block-level indent
# that tex4ht inherited from the surrounding LaTeX (\item, \enumerate)).
# tex4ht renders spaces as NBSP (U+00A0) inside ectt spans.
CODE_SPAN_LEADING_WS = re.compile(
    r"(<span class='ectt-\d+'>)([ \t\xa0]+)"
)


def fix_line_numbers(html: str) -> str:
    # Case A: digits followed by spaces inside the span -> close span before
    # the spaces, drop the artificial 4/7 shrinkage. The CSS now reserves
    # enough room via min-width + padding-right, so we keep the original
    # space count as the indent signal.
    html = LINENO_SPAN_WITH_SPACES.sub(
        lambda m: "<span class='ecrm-0500'>" + m.group(1) + "</span>" + m.group(2),
        html,
    )
    # Case B: digits followed by a newline -> move the newline outside.
    html = LINENO_SPAN_NEWLINE.sub(
        lambda m: "<span class='ecrm-0500'>" + m.group(1) + "</span>\n",
        html,
    )
    # Case C: bare digits with no trailing whitespace and a code span (or
    # other text) immediately after -- guarantee a single separator space so
    # the line number visually detaches from the code.
    html = LINENO_SPAN_BARE.sub(
        lambda m: "<span class='ecrm-0500'>" + m.group(1) + "</span> ",
        html,
    )
    return html


LINENO_BEFORE_CODE = re.compile(
    r"(<span class='ecrm-0500'>\d+</span>)([ ]*)(?=<span class='ectt-)"
)


def normalize_lineno_gap(body: str) -> str:
    """Even out the lineno-to-code gap inside a pre block.

    tex4ht emits inconsistent padding inside the line-number span (the
    LINENO_SPAN_WITH_SPACES pass moves it outside, so it ends up between
    `</span>` and the next code span). For deeply-indented source, that
    gap carries real indentation we must preserve. For simpler blocks
    tex4ht still emits stray padding on some lines and not others,
    producing visible misalignment.

    Two-step heuristic:
      1. If every gap in the block is small (max <= 2), flatten them all
         to a single space -- the whole block has no source indent.
      2. Otherwise compute the modal gap. Outlier lines whose gap exceeds
         the mode by 3 or less are tex4ht artifacts (the "first-line of
         a nested block" quirk); pull them back to the mode. Outliers
         that exceed the mode by 4+ are real source indent (e.g. deep
         struct fields); leave those alone.
    """
    matches = list(LINENO_BEFORE_CODE.finditer(body))
    if not matches:
        return body
    gaps = [len(m.group(2)) for m in matches]
    if max(gaps) <= 2:
        return LINENO_BEFORE_CODE.sub(r"\1 ", body)

    # The minimum gap represents the block's "structural" indent (i.e.,
    # how much padding tex4ht emits when the source line is unindented).
    # Lines whose gap exceeds the minimum by 1-3 are first-line / nested-
    # block artifacts -- pull them back. Lines that exceed by 4+ are real
    # source indent (e.g. deep struct fields); leave those alone.
    min_gap = min(gaps)

    def replace(m: re.Match) -> str:
        gap = len(m.group(2))
        excess = gap - min_gap
        if 0 < excess <= 3:
            return m.group(1) + (" " * min_gap)
        return m.group(0)

    return LINENO_BEFORE_CODE.sub(replace, body)


def strip_block_level_indent(body: str) -> str:
    """Strip uniform leading whitespace from every code span in a block.

    When a \\begin{codebash}...\\end{codebash} is nested inside an \\item
    or other indented LaTeX context, every line of its source content
    starts with the same prefix (e.g. 4 spaces). tex4ht renders that
    prefix as NBSP inside each code span, making the block look indented
    when it should be left-aligned. Detect the common leading whitespace
    and strip it.
    """
    leads = []
    for m in CODE_SPAN_LEADING_WS.finditer(body):
        leads.append(len(m.group(2)))
    # If not every code span has a leading-whitespace match, some lines
    # have zero leading -- there's no common block-level indent to strip.
    code_spans = len(re.findall(r"<span class='ectt-\d+'>(?=[^<])", body))
    if not leads or len(leads) < code_spans:
        return body
    common = min(leads)
    if common <= 0:
        return body

    def shrink(m: re.Match) -> str:
        whitespace = m.group(2)
        return m.group(1) + whitespace[common:]

    return CODE_SPAN_LEADING_WS.sub(shrink, body)


def clean_fancyvrb_blocks(html: str) -> str:
    def scrub(match: re.Match) -> str:
        open_tag, body, close_tag = match.group(1), match.group(2), match.group(3)
        # Strip the per-line column-padding that tex4ht emits inside code
        # spans (e.g. "code   </span>" -> "code</span>"). Do this first so
        # the merged-lines detection below sees clean span boundaries.
        body = CODE_SPAN_TRAILING_WS.sub(r"\1</span>", body)
        # Split logical lines that tex4ht inlined into one HTML line: a
        # lineno span starting immediately after the previous </span>
        # (without an intervening newline) means two source lines were
        # collapsed. Insert a newline between them.
        body = MERGED_LINES.sub(r"</span>\n\1", body)
        # Strip leading whitespace at the very start of the pre body
        # (tex4ht emits it when the source \begin{codebash} line was
        # itself indented, e.g. inside an \item).
        body = body.lstrip(" \t")
        # Even out tex4ht's inconsistent lineno-to-code padding.
        body = normalize_lineno_gap(body)
        # If every code line shares the same leading-whitespace prefix
        # (a \item / nested-block indent leak), strip the common prefix.
        body = strip_block_level_indent(body)

        lines = body.split("\n")
        cleaned = [line.rstrip(" \t") for line in lines]
        # Drop whitespace-only lines that don't carry a line-number span --
        # those are page-break / overfull-vbox artifacts. A legitimate blank
        # code line still carries its own ecrm-0500 span and survives.
        filtered = [
            line
            for line in cleaned
            if line.strip() != "" or "ecrm-0500" in line
        ]
        return open_tag + "\n".join(filtered) + close_tag

    return FANCYVRB_BLOCK.sub(scrub, html)


MOBILE_NAV_BUTTON = """
<button class="toc-toggle" type="button" aria-label="Open table of contents" aria-controls="lkmpg-toc" aria-expanded="false">
<span></span><span></span><span></span>
</button>
<div class="toc-backdrop" hidden></div>
"""

MOBILE_NAV_SCRIPT = """
<script>
document.addEventListener('DOMContentLoaded', function(){
  var btn = document.querySelector('.toc-toggle');
  var backdrop = document.querySelector('.toc-backdrop');
  var toc = document.querySelector('.tableofcontents');
  if (!btn) return;
  if (toc && !toc.id) toc.id = 'lkmpg-toc';
  function setOpen(open){
    document.body.classList.toggle('toc-open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (backdrop) backdrop.hidden = !open;
  }
  btn.addEventListener('click', function(e){
    e.preventDefault();
    setOpen(!document.body.classList.contains('toc-open'));
  });
  if (backdrop) backdrop.addEventListener('click', function(){ setOpen(false); });
  if (toc) toc.addEventListener('click', function(e){
    var t = e.target;
    while (t && t !== toc) {
      if (t.tagName === 'A') { setOpen(false); break; }
      t = t.parentNode;
    }
  });
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape') setOpen(false);
  });
});
</script>
"""


def inject_mobile_nav(html: str) -> str:
    """Insert hamburger toggle button + backdrop right after <body>, and the
    script before </body> so .tableofcontents has been parsed by the time
    the click handlers are attached."""
    if "toc-toggle" in html:
        return html
    html = html.replace("<body>", "<body>" + MOBILE_NAV_BUTTON, 1)
    html = html.replace("</body>", MOBILE_NAV_SCRIPT + "</body>", 1)
    return html


def process(html: str) -> str:
    html = fix_line_numbers(html)
    html = clean_fancyvrb_blocks(html)
    html = inject_mobile_nav(html)
    return html


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: postprocess_html.py <file.html>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    src = path.read_text(encoding="utf-8")
    path.write_text(process(src), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
