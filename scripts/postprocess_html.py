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

    min_gap = min(gaps)

    # If every line has the SAME gap and it's > 1, this is block-level
    # indent leak (the whole block was emitted with uniform extra
    # whitespace because the source \begin{codebash} was nested inside an
    # \item or similar indented LaTeX context). Strip it down to a
    # single-space baseline.
    if min_gap == max(gaps) and min_gap > 1:
        return LINENO_BEFORE_CODE.sub(r"\1 ", body)

    # The minimum gap represents the block's "structural" indent (i.e.,
    # how much padding tex4ht emits when the source line is unindented).
    # Lines whose gap exceeds the minimum by 1-3 are first-line / nested-
    # block artifacts -- pull them back. Lines that exceed by 4+ are real
    # source indent (e.g. deep struct fields); leave those alone.
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


TOC_BLOCK = re.compile(
    r"(<div class='tableofcontents'>)(.*?)(\s*</div>\s*\n)",
    re.DOTALL,
)
TOC_ENTRY = re.compile(
    r"<span class='(chapterToc|sectionToc|subsectionToc)'>.*?</span>",
    re.DOTALL,
)


def restructure_toc(html: str) -> str:
    """Group each chapter's sections + subsections under a chapter-group div
    so we can collapse them with CSS + scroll-spy JS (mdBook-style)."""
    m = TOC_BLOCK.search(html)
    if not m:
        return html
    opening, body, closing = m.group(1), m.group(2), m.group(3)
    if "toc-chapter-group" in body:
        return html  # already restructured

    entries = []
    for em in TOC_ENTRY.finditer(body):
        entries.append((em.group(1), em.group(0)))

    if not entries:
        return html

    groups = []
    current = None
    for cls, entry in entries:
        if cls == "chapterToc":
            if current is not None:
                groups.append(current)
            current = {"chapter": entry, "children": []}
        else:
            if current is None:
                # Section before any chapter (TOC opens with a sectionToc).
                # Wrap as a synthetic group with no chapter header.
                current = {"chapter": "", "children": []}
            current["children"].append(entry)
    if current is not None:
        groups.append(current)

    new_body_parts = []
    for g in groups:
        chapter_html = g["chapter"]
        if g["children"] and chapter_html:
            # Inject expand chevron inside the chapterToc span (before its
            # text). Chevron toggles the chapter group's manual-expand state
            # without navigating.
            chevron = (
                '<button class="toc-expand" type="button" '
                'aria-label="Expand chapter sections" aria-expanded="false">'
                '<span class="toc-chevron"></span></button>'
            )
            chapter_html = re.sub(
                r"(<span class='chapterToc'>)",
                r"\1" + chevron,
                chapter_html,
                count=1,
            )
            children_html = "\n".join(g["children"])
            new_body_parts.append(
                f'<div class="toc-chapter-group">{chapter_html}'
                f'<div class="toc-chapter-sections">{children_html}</div></div>'
            )
        elif chapter_html:
            new_body_parts.append(
                f'<div class="toc-chapter-group">{chapter_html}</div>'
            )
        else:
            children_html = "\n".join(g["children"])
            new_body_parts.append(
                f'<div class="toc-chapter-group">{children_html}</div>'
            )

    new_body = "\n".join(new_body_parts)
    return html.replace(m.group(0), opening + "\n" + new_body + closing, 1)


SCROLL_SPY_SCRIPT = """
<script>
document.addEventListener('DOMContentLoaded', function(){
  var toc = document.querySelector('.tableofcontents');
  if (!toc) return;
  var links = toc.querySelectorAll('a[href^="#"]');
  if (!links.length) return;
  var linkByHash = Object.create(null);
  links.forEach(function(a){
    var h = a.getAttribute('href');
    if (h && h.length > 1) linkByHash[h.slice(1)] = a;
  });

  // Chevron click: toggle user-expanded on its chapter-group, independent of
  // scroll-spy. Stops propagation so the chapter link isn't followed.
  toc.addEventListener('click', function(e){
    var btn = e.target.closest('.toc-expand');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    var group = btn.closest('.toc-chapter-group');
    if (!group) return;
    var nowOpen = !group.classList.contains('user-expanded');
    group.classList.toggle('user-expanded', nowOpen);
    btn.setAttribute('aria-expanded', nowOpen ? 'true' : 'false');
  });

  var headings = Array.prototype.slice.call(
    document.querySelectorAll('h2[id], h3[id], h4[id]')
  ).filter(function(h){ return h.id !== 'contents'; });
  if (!headings.length) return;

  function setActiveByHash(id){
    var link = linkByHash[id];
    if (!link) return;
    // Clear previous scroll-set highlight + scroll-expand. Leave user-expand
    // (the chevron-toggle state) untouched.
    toc.querySelectorAll('.toc-active').forEach(function(el){
      el.classList.remove('toc-active');
    });
    toc.querySelectorAll('.toc-chapter-group.scroll-expanded').forEach(function(el){
      el.classList.remove('scroll-expanded');
    });
    var span = link.closest('.chapterToc, .sectionToc, .subsectionToc');
    if (span) span.classList.add('toc-active');
    var group = link.closest('.toc-chapter-group');
    if (group) group.classList.add('scroll-expanded');
    // Scroll the active entry into view in the TOC sidebar (desktop only).
    if (span && span.scrollIntoView && !document.body.classList.contains('toc-open')) {
      var r = span.getBoundingClientRect();
      var tocRect = toc.getBoundingClientRect();
      if (r.top < tocRect.top || r.bottom > tocRect.bottom) {
        span.scrollIntoView({block: 'center'});
      }
    }
  }

  var currentId = null;
  function update(){
    var offset = window.innerHeight * 0.30;
    var nearest = null;
    for (var i = 0; i < headings.length; i++) {
      var h = headings[i];
      var top = h.getBoundingClientRect().top;
      if (top < offset) nearest = h;
      else break;
    }
    var id = nearest ? nearest.id : (headings[0] && headings[0].id);
    if (id && id !== currentId) {
      currentId = id;
      setActiveByHash(id);
    }
  }

  var raf = null;
  window.addEventListener('scroll', function(){
    if (raf) return;
    raf = window.requestAnimationFrame(function(){
      raf = null;
      update();
    });
  }, { passive: true });
  update();
});
</script>
"""


def inject_scroll_spy(html: str) -> str:
    if "linkByHash" in html:
        return html
    return html.replace("</body>", SCROLL_SPY_SCRIPT + "</body>", 1)


def process(html: str) -> str:
    html = fix_line_numbers(html)
    html = clean_fancyvrb_blocks(html)
    html = restructure_toc(html)
    html = inject_mobile_nav(html)
    html = inject_scroll_spy(html)
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
