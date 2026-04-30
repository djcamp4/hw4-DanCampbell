#!/usr/bin/env python3
"""Resume extractor + adversarial-input detector.

Runs at the very start of the applicant-scoring pipeline, BEFORE any
LLM sees the document. Reads PDF / DOCX / HTML / TXT, emits clean text
with hidden adversarial content stripped, and writes a metadata sidecar
describing what was found and what the downstream agent should do.

Per-file outputs (next to the source, or under --out-dir):
  <basename>.txt        clean text safe to feed to the scoring LLM
  <basename>.meta.json  metadata + flags + recommended action

Batch output (when given a directory):
  extraction_report.csv  one row per file: action, flags, counts

Recommended actions:
  extract  text looks like a resume; pass to scorer
  review   resume-shaped but suspicious; human must look before scoring
  refuse   not a resume (no extractable text, too short, wrong shape)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import docx as docx_lib
except ImportError:
    docx_lib = None


# --- Heuristics --------------------------------------------------------------

# Phrases commonly used in prompt-injection attacks on resume scorers.
INJECTION_PATTERNS = [
    r"ignore\b[^.\n]{0,60}?\b(instructions?|prompts?|rules?|system\s+prompt)",
    r"disregard\b[^.\n]{0,60}?\b(instructions?|prompts?|rules?|system\s+prompt|rubric)",
    r"you are now [a-z ]{0,40}(?:assistant|scorer|recruiter)",
    r"override\b[^.\n]{0,60}?\b(instructions?|system\s+prompt|rules?)",
    r"(give|assign|return)\b[^.\n]{0,80}?\b(highest|maximum|perfect|top)\s+(possible\s+)?score",
    r"score\b[^.\n]{0,40}?\b(a |as )?5\b",
    r"as (an|the) ai language model",
    r"system\s*:\s*you\b",
]
INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in INJECTION_PATTERNS), re.IGNORECASE)

# Sections expected on a real resume. Used to flag "doesn't look like a resume."
RESUME_SECTION_HINTS = [
    "experience", "education", "skills", "employment",
    "work history", "summary", "objective", "projects",
    "certifications", "qualifications",
]

MIN_RESUME_CHARS = 300        # below this we suspect non-resume content
MIN_RESUME_WORDS = 60
HIDDEN_FONT_SIZE_PT = 1.0     # text smaller than this is treated as hidden
WHITE_LUMINANCE = 0.95        # treat near-white text as hidden when no bg evidence


@dataclass
class FileMeta:
    path: str
    file_type: str
    char_count: int = 0
    word_count: int = 0
    page_count: int = 0
    flags: list[str] = field(default_factory=list)
    hidden_text_excerpts: list[str] = field(default_factory=list)
    injection_phrases: list[str] = field(default_factory=list)
    action: str = "extract"
    error: str | None = None


# --- PDF extraction ----------------------------------------------------------

def _luminance(rgb_int: int) -> float:
    """Approximate perceived luminance of a packed sRGB integer."""
    r = ((rgb_int >> 16) & 0xFF) / 255.0
    g = ((rgb_int >> 8) & 0xFF) / 255.0
    b = (rgb_int & 0xFF) / 255.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def extract_pdf(path: Path, meta: FileMeta) -> str:
    if fitz is None:
        meta.error = "PyMuPDF not installed; run: pip install pymupdf"
        meta.action = "refuse"
        return ""
    try:
        doc = fitz.open(path)
    except Exception as e:
        meta.error = f"pdf open failed: {e}"
        meta.action = "refuse"
        return ""

    visible_chunks: list[str] = []
    hidden_chunks: list[str] = []
    meta.page_count = len(doc)

    for page in doc:
        # Background color heuristic: assume white unless we know otherwise.
        # (Detecting the actual page background reliably requires rendering;
        # almost all resumes use white pages, so we treat near-white text as hidden.)
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not text.strip():
                        continue
                    color = span.get("color", 0)
                    size = span.get("size", 0.0)
                    lum = _luminance(color)
                    is_hidden = (
                        lum >= WHITE_LUMINANCE  # white-on-white
                        or size < HIDDEN_FONT_SIZE_PT  # microscopic font
                    )
                    if is_hidden:
                        hidden_chunks.append(text)
                    else:
                        visible_chunks.append(text)
                visible_chunks.append("\n")
            visible_chunks.append("\n")

    if hidden_chunks:
        meta.flags.append("hidden_text_detected")
        # Save excerpts (truncated) so a human reviewer can audit.
        meta.hidden_text_excerpts = [
            (t[:200] + ("…" if len(t) > 200 else "")) for t in hidden_chunks[:10]
        ]

    return "".join(visible_chunks)


# --- DOCX extraction ---------------------------------------------------------

def extract_docx(path: Path, meta: FileMeta) -> str:
    if docx_lib is None:
        meta.error = "python-docx not installed; run: pip install python-docx"
        meta.action = "refuse"
        return ""
    try:
        doc = docx_lib.Document(str(path))
    except Exception as e:
        meta.error = f"docx open failed: {e}"
        meta.action = "refuse"
        return ""

    visible: list[str] = []
    hidden: list[str] = []
    for para in doc.paragraphs:
        for run in para.runs:
            text = run.text
            if not text.strip():
                continue
            color = None
            try:
                if run.font.color and run.font.color.rgb is not None:
                    color = int(str(run.font.color.rgb), 16)
            except Exception:
                color = None
            size_pt = run.font.size.pt if run.font.size else None
            is_hidden = False
            if run.font.hidden:
                is_hidden = True
            if color is not None and _luminance(color) >= WHITE_LUMINANCE:
                is_hidden = True
            if size_pt is not None and size_pt < HIDDEN_FONT_SIZE_PT:
                is_hidden = True
            (hidden if is_hidden else visible).append(text)
        visible.append("\n")

    if hidden:
        meta.flags.append("hidden_text_detected")
        meta.hidden_text_excerpts = [
            (t[:200] + ("…" if len(t) > 200 else "")) for t in hidden[:10]
        ]
    return "".join(visible)


# --- HTML / TXT extraction ---------------------------------------------------

HTML_HIDDEN_STYLE = re.compile(
    r"(display\s*:\s*none|visibility\s*:\s*hidden|color\s*:\s*#?(fff|ffffff|white)|font-size\s*:\s*0)",
    re.IGNORECASE,
)
HTML_TAG = re.compile(r"<[^>]+>")
HTML_STYLE_OR_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


HIDDEN_ELEMENT_RE = re.compile(
    r'<(?P<tag>[a-z][a-z0-9]*)\b[^>]*style\s*=\s*"[^"]*'
    r'(?:display\s*:\s*none|visibility\s*:\s*hidden|color\s*:\s*#?(?:fff|ffffff|white)|font-size\s*:\s*0)'
    r'[^"]*"[^>]*>(?P<inner>.*?)</(?P=tag)\s*>',
    re.IGNORECASE | re.DOTALL,
)


def extract_html(path: Path, meta: FileMeta) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")

    # Detect AND remove any hidden-style elements before tags are stripped.
    hidden_text: list[str] = []

    def _capture_and_drop(m: re.Match) -> str:
        inner = re.sub(r"<[^>]+>", " ", m.group("inner")).strip()
        if inner:
            hidden_text.append(inner)
        return " "  # remove the whole element from the visible output

    cleaned = HIDDEN_ELEMENT_RE.sub(_capture_and_drop, raw)

    if hidden_text:
        meta.flags.append("hidden_text_detected")
        meta.hidden_text_excerpts = [t[:200] for t in hidden_text[:10]]

    # Strip script/style blocks, then tags.
    no_blocks = HTML_STYLE_OR_SCRIPT.sub("", cleaned)
    text = HTML_TAG.sub(" ", no_blocks)
    return re.sub(r"\s+", " ", text).strip()


def extract_txt(path: Path, meta: FileMeta) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# --- Refusal / review classification -----------------------------------------

def classify(text: str, meta: FileMeta) -> None:
    """Set meta.action and append flags based on content + earlier flags."""
    if meta.error:
        return  # action already set to refuse

    meta.char_count = len(text)
    meta.word_count = len(text.split())

    if meta.char_count < MIN_RESUME_CHARS or meta.word_count < MIN_RESUME_WORDS:
        meta.flags.append("too_short_for_resume")
        meta.action = "refuse"
        return

    lc = text.lower()
    section_hits = sum(1 for h in RESUME_SECTION_HINTS if h in lc)
    if section_hits == 0:
        meta.flags.append("no_resume_sections_found")
        meta.action = "refuse"
        return
    if section_hits == 1:
        meta.flags.append("only_one_resume_section")

    # Injection scan over the COMBINED visible + hidden text. The whole
    # point is to catch attacks even if the visible text alone looks fine.
    full = text + "\n" + "\n".join(meta.hidden_text_excerpts)
    found = sorted(set(m.group(0).strip() for m in INJECTION_RE.finditer(full)))
    if found:
        meta.injection_phrases = found
        meta.flags.append("possible_prompt_injection")

    if "hidden_text_detected" in meta.flags or "possible_prompt_injection" in meta.flags:
        meta.action = "review"


# --- Orchestration -----------------------------------------------------------

EXTRACTORS = {
    ".pdf": extract_pdf,
    ".docx": extract_docx,
    ".html": extract_html,
    ".htm": extract_html,
    ".txt": extract_txt,
}


def process_file(path: Path, out_dir: Path) -> FileMeta:
    ext = path.suffix.lower()
    meta = FileMeta(path=str(path), file_type=ext.lstrip("."))
    if ext not in EXTRACTORS:
        meta.error = f"unsupported file type: {ext}"
        meta.action = "refuse"
        meta.flags.append("unsupported_type")
        return meta

    text = EXTRACTORS[ext](path, meta)
    classify(text, meta)

    base = out_dir / path.stem
    base.parent.mkdir(parents=True, exist_ok=True)
    if meta.action != "refuse" and text.strip():
        base.with_suffix(".txt").write_text(text)
    base.with_suffix(".meta.json").write_text(json.dumps(asdict(meta), indent=2, sort_keys=True))
    return meta


def write_report(metas: list[FileMeta], out_dir: Path) -> Path:
    path = out_dir / "extraction_report.csv"
    metas_sorted = sorted(metas, key=lambda m: (m.action, m.path))
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["filename", "action", "file_type", "char_count", "word_count", "flags"])
        for m in metas_sorted:
            w.writerow([
                Path(m.path).name,
                m.action,
                m.file_type,
                m.char_count,
                m.word_count,
                ";".join(m.flags),
            ])
    return path


# --- Live, agent-style terminal output ---------------------------------------

class C:
    """ANSI color codes; auto-disabled when stdout is not a terminal."""
    ENABLE = sys.stdout.isatty()
    RESET = "\033[0m" if ENABLE else ""
    BOLD = "\033[1m" if ENABLE else ""
    DIM = "\033[2m" if ENABLE else ""
    GREEN = "\033[32m" if ENABLE else ""
    YELLOW = "\033[33m" if ENABLE else ""
    RED = "\033[31m" if ENABLE else ""
    CYAN = "\033[36m" if ENABLE else ""
    GRAY = "\033[90m" if ENABLE else ""


ACTION_STYLE = {
    "extract": (C.GREEN, "extract", "safe to score"),
    "review":  (C.YELLOW, "review",  "human required"),
    "refuse":  (C.RED,    "refuse",  "not a resume"),
}


def _say(line: str = "") -> None:
    print(line, flush=True)


def _live_file_trace(idx: int, total: int, path: Path, meta: FileMeta) -> None:
    color, label, note = ACTION_STYLE[meta.action]
    _say(f"{C.BOLD}[{idx}/{total}]{C.RESET} {path.name}")
    _say(f"  {C.GRAY}├─{C.RESET} format: {meta.file_type}"
         + (f", {meta.page_count} page(s)" if meta.page_count else ""))
    if meta.error:
        _say(f"  {C.GRAY}├─{C.RESET} {C.RED}error:{C.RESET} {meta.error}")
    else:
        _say(f"  {C.GRAY}├─{C.RESET} extracted {meta.char_count} chars / {meta.word_count} words")

    if "hidden_text_detected" in meta.flags:
        _say(f"  {C.GRAY}├─{C.RESET} {C.YELLOW}⚠ hidden text detected{C.RESET}"
             f" ({len(meta.hidden_text_excerpts)} excerpt(s))")
        for ex in meta.hidden_text_excerpts[:1]:
            preview = ex.replace("\n", " ")[:100]
            _say(f"  {C.GRAY}│{C.RESET}    {C.DIM}\"{preview}…\"{C.RESET}")
    if meta.injection_phrases:
        _say(f"  {C.GRAY}├─{C.RESET} {C.YELLOW}⚠ injection phrases:{C.RESET}")
        for phrase in meta.injection_phrases[:3]:
            _say(f"  {C.GRAY}│{C.RESET}    - {phrase}")
    if "too_short_for_resume" in meta.flags:
        _say(f"  {C.GRAY}├─{C.RESET} {C.RED}✗ too short to be a resume{C.RESET}")
    if "no_resume_sections_found" in meta.flags:
        _say(f"  {C.GRAY}├─{C.RESET} {C.RED}✗ no resume sections found{C.RESET}")

    icon = {"extract": "✅", "review": "🔶", "refuse": "⛔"}[meta.action]
    _say(f"  {C.GRAY}└─{C.RESET} {icon} {color}{C.BOLD}{label}{C.RESET} — {note}")
    _say()


def _bar(count: int, total: int, width: int = 28) -> str:
    if total <= 0:
        return "·" * width
    filled = round(count / total * width)
    return "█" * filled + "░" * (width - filled)


def _live_summary(metas: list[FileMeta], report_path: Path, out_dir: Path) -> None:
    total = len(metas)
    counts = {"extract": 0, "review": 0, "refuse": 0}
    by_format: dict[str, int] = {}
    for m in metas:
        counts[m.action] = counts.get(m.action, 0) + 1
        by_format[m.file_type] = by_format.get(m.file_type, 0) + 1

    _say(f"{C.BOLD}{'═' * 58}{C.RESET}")
    _say(f"{C.BOLD}SUMMARY{C.RESET} — {total} file(s) processed")
    _say()
    _say(f"  {'By action':<10}")
    for action in ("extract", "review", "refuse"):
        color, label, _ = ACTION_STYLE[action]
        _say(f"  {color}{label:<8}{C.RESET} {_bar(counts[action], total)}  "
             f"{counts[action]} / {total}")
    _say()
    _say(f"  {'By format':<10}")
    for fmt in sorted(by_format):
        _say(f"  {C.CYAN}{fmt:<8}{C.RESET} {_bar(by_format[fmt], total)}  "
             f"{by_format[fmt]} / {total}")
    _say()

    for action in ("extract", "review", "refuse"):
        bucket = [m for m in metas if m.action == action]
        if not bucket:
            continue
        color, label, note = ACTION_STYLE[action]
        icon = {"extract": "✅", "review": "🔶", "refuse": "⛔"}[action]
        _say(f"{icon} {color}{C.BOLD}{label.upper()}{C.RESET} ({len(bucket)}) — {note}:")
        for m in sorted(bucket, key=lambda x: Path(x.path).name):
            name = Path(m.path).name
            detail = ", ".join(m.flags) if m.flags else f"{m.char_count} chars, {m.word_count} words"
            _say(f"  • {name:<32} {C.GRAY}{detail}{C.RESET}")
        _say()

    _say(f"{C.GRAY}📄 Full CSV report:{C.RESET} {report_path}")
    _say(f"{C.GRAY}📂 Cleaned text:   {C.RESET} {out_dir}/*.txt")
    _say(f"{C.GRAY}🔍 Per-file metadata:{C.RESET} {out_dir}/*.meta.json")


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract resumes; flag adversarial inputs.")
    ap.add_argument("path", type=Path, help="file or directory of applicant files")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="where to write outputs (default: ./extracted/ next to input)")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress live trace and only print a one-line summary to stderr")
    args = ap.parse_args()

    if not args.path.exists():
        print(f"error: not found: {args.path}", file=sys.stderr)
        return 2

    if args.path.is_file():
        files = [args.path]
        out_dir = args.out_dir or args.path.parent / "extracted"
    else:
        out_dir = args.out_dir or args.path / "extracted"
        out_dir_resolved = out_dir.resolve()
        files = sorted(
            p for p in args.path.rglob("*")
            if p.is_file()
            and p.suffix.lower() in EXTRACTORS
            and out_dir_resolved not in p.resolve().parents
        )

    if not files:
        print("error: no supported files found", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.quiet:
        _say(f"{C.BOLD}{C.CYAN}🔍 resume-extractor{C.RESET} "
             f"— analyzing {len(files)} file(s) in {C.BOLD}{args.path}{C.RESET}")
        _say()

    metas: list[FileMeta] = []
    for i, p in enumerate(files, start=1):
        meta = process_file(p, out_dir)
        metas.append(meta)
        if not args.quiet:
            _live_file_trace(i, len(files), p, meta)

    report_path = write_report(metas, out_dir)

    if args.quiet:
        counts = {"extract": 0, "review": 0, "refuse": 0}
        for m in metas:
            counts[m.action] = counts.get(m.action, 0) + 1
        print(f"processed {len(metas)} file(s): "
              f"{counts['extract']} ok, {counts['review']} flagged, {counts['refuse']} refused",
              file=sys.stderr)
        print(f"report: {report_path}", file=sys.stderr)
        print(f"outputs: {out_dir}", file=sys.stderr)
    else:
        _live_summary(metas, report_path, out_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
