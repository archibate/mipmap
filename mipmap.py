#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""mipmap — progressive disclosure summarization via local LLM.

Reads text from stdin (or a file) and emits a stack of summaries at
progressively larger sizes, smallest first. Inspired by texture mipmaps:
each level roughly doubles in size, capped at ~15% of the source. The
1-sentence headline appears in ~1s on a warm local model; further levels
stream in behind it. Stop reading whenever you have enough.

Defaults target qwen2.5-coder:14b on a local ollama at localhost:11434.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Iterator

# --- defaults ----------------------------------------------------------------

DEFAULT_MODEL = "qwen2.5-coder:14b"
DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_FLOOR_LATIN = 20
DEFAULT_FLOOR_CJK = 30
DEFAULT_COMPRESSION = 0.15
DEFAULT_MAX_LEVELS = 7
DEFAULT_RATIO = 2.0  # each level is this many times the prior; mipmap-style doubling
DEFAULT_TEMP = 0.4
# When --num-ctx is not given, mipmap queries the model's modelfile via
# /api/show; this fallback is used only if that query fails.
FALLBACK_NUM_CTX = 8192
# --max-chars defaults to auto: ~num_ctx*3 for Latin, ~num_ctx*0.8 for CJK
# (CJK tokenizes 1 char ≈ 1 token; Latin is ~3-4 chars per token)
LATIN_CHARS_PER_TOKEN = 3.0
CJK_CHARS_PER_TOKEN = 0.8
# Tokens reserved for prompt scaffolding + output budget when computing
# max-chars. Adaptive: ~25% of context with a 1500-token floor so small-ctx
# models don't get squeezed (e.g. at num_ctx=4096 we only reserve 1500, not
# the full 3000 a fixed budget would force).
def reserved_tokens_for(num_ctx: int) -> int:
    return max(1500, num_ctx // 4)

# --- counters & detection ----------------------------------------------------

ASCII_WORD = re.compile(r"[A-Za-z0-9]+(?:[-_'][A-Za-z0-9]+)*")
CJK_CHAR = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
# Match a level marker line — be permissive about decorations.
# Accepts "--- LEVEL 1 ---", "LEVEL 1", "## Level 1", "**LEVEL 1**", etc.
# Must end with \n so we don't match partial lines while streaming.
LVL_DELIM = re.compile(
    r"^[\s\-=#*_~]*LEVEL\s+(\d+)\s*[\s\-=#*_~:]*\n",
    re.MULTILINE | re.IGNORECASE,
)
# Markdown horizontal rule that some models emit between label and content.
LEADING_HR = re.compile(r"\A[-*=_]{3,}[ \t]*(?:\n|$)")
# A line that is just a markdown code-fence (optionally with a language tag).
# Some models wrap the entire mipmap in ```...``` despite the prompt; strip those.
FENCE_LINE = re.compile(r"\A\s*```\w*\s*\Z")

def count_units(s: str) -> tuple[int, int]:
    return len(ASCII_WORD.findall(s)), len(CJK_CHAR.findall(s))

def is_cjk_dominant(s: str, override: str = "auto") -> bool:
    if override == "zh":
        return True
    if override == "en":
        return False
    a, c = count_units(s)
    return (c / (a + c)) > 0.5 if (a + c) else False

# --- level computation -------------------------------------------------------

def calibrated_levels(units: int, floor: int, compression: float, cap: int,
                       ratio: float = DEFAULT_RATIO) -> list[int]:
    """Return target sizes per level, smallest first.

    Behavior by source size:
      units < floor:          [units]   — too short, pass through verbatim
      ceiling < floor:        [floor]   — single TLDR; full mipmap not useful
      otherwise:              [floor, ratio*floor, ratio^2*floor, ..., ceiling]
    where ceiling = int(units * compression).
    """
    if units <= 0:
        return []
    if units < floor:
        return [units]
    ceiling = int(units * compression)
    if ceiling < floor:
        return [floor]
    out: list[int] = []
    n: float = floor
    while round(n) <= ceiling and len(out) < cap:
        out.append(round(n))
        next_n = max(n + 1, n * ratio)  # ensure monotonic growth even at ratio≈1
        n = next_n
    if out and out[-1] < ceiling and ceiling - out[-1] >= floor and len(out) < cap:
        out.append(ceiling)
    return out

# --- prompt template ---------------------------------------------------------

L1_EN = (
    "Write ONE sentence — just one, no semicolons or periods combining ideas. "
    "Convey the source's main claim, recommendation, finding, or definition. "
    "Use declarative or imperative voice. Do not begin with topic-announcement "
    "phrasing like 'The source discusses/presents/explores/describes/covers/"
    "outlines/is about'. State the content directly, in your own voice."
)

def later_instruction(prior_level: int) -> str:
    return (
        f"INCLUDE everything from LEVEL {prior_level} (rephrased in fresh "
        f"wording, not copied verbatim), THEN add more facts, details, "
        f"examples, or context from the source. Same topics, more depth — "
        f"do NOT switch to a different section or aspect of the source. "
        f"The output must be visibly longer than LEVEL {prior_level}."
    )

def make_prompt(src: str, levels: list[int], cjk: bool) -> str:
    """Build the LLM prompt. Always English-language instructions — the model
    follows them more reliably than Chinese instructions, and naturally
    responds in the source's language regardless of the prompt language.
    The `cjk` flag only affects the per-level unit label (字 vs. words)."""
    unit_label = "字" if cjk else "w"
    spec = ", ".join(f"LEVEL {i+1} (~{w}{unit_label})" for i, w in enumerate(levels))
    intro = [
        "Your task is to produce a 'mipmap' summary of the source below: "
        "a stack of summaries at progressively larger sizes, smallest first. "
        "Respond in the same language as the source.",
        "",
        "The source may contain code, tables, lists, or other structured "
        "content. Summarize their meaning naturally; do not attempt to "
        "reproduce them verbatim.",
    ]
    if len(levels) >= 2:
        fmt_lines = [
            "Your output MUST begin with a line containing exactly `--- LEVEL 1 ---`, "
            "followed by the LEVEL 1 content. Then a line `--- LEVEL 2 ---`, "
            "followed by the LEVEL 2 content. And so on. Each delimiter must be "
            "on its own line. Do NOT reproduce the source's structure (headings, "
            "tables, lists). Do NOT wrap your output in markdown code fences "
            "(```), even if the source contains them. Output ONLY the mipmap "
            "levels separated by these delimiters as plain text.",
        ]
    else:
        fmt_lines = [
            "Your output MUST begin with a line containing exactly `--- LEVEL 1 ---`, "
            "followed by the LEVEL 1 content. There is only one level. Do NOT "
            "reproduce the source's structure (headings, tables, lists). Do NOT "
            "wrap your output in markdown code fences (```). Output ONLY the "
            "LEVEL 1 line and its content as plain text.",
        ]
    outro = [
        "",
        "—— OUTPUT FORMAT (MANDATORY) ——",
        "",
        *fmt_lines,
        "",
        f"Levels in order: {spec}.",
        "",
    ]
    for i, w in enumerate(levels):
        instr = L1_EN if i == 0 else later_instruction(i)
        outro.append(f"LEVEL {i+1}: approximately {w} {unit_label}. " + instr)
    outro += ["", "Begin output now (start with `--- LEVEL 1 ---`)."]
    return "\n".join(intro + ["", "<source>", src, "</source>"] + outro)

# --- ollama streaming --------------------------------------------------------

NUM_CTX_RE = re.compile(r"^\s*num_ctx\s+(\d+)\s*$", re.MULTILINE)

def query_model_num_ctx(endpoint: str, model: str, timeout: float = 2.0) -> int | None:
    """Ask ollama what num_ctx is set in the modelfile. Returns None on any
    failure (server unreachable, model missing, num_ctx not specified)."""
    body = json.dumps({"name": model}).encode()
    url = endpoint.rstrip("/") + "/api/show"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    params = data.get("parameters", "")
    m = NUM_CTX_RE.search(params)
    return int(m.group(1)) if m else None


def stream_raw(endpoint: str, model: str, prompt: str, temperature: float,
               seed: int | None, num_ctx: int) -> Iterator[str]:
    options: dict = {"temperature": temperature, "num_ctx": num_ctx}
    if seed is not None:
        options["seed"] = seed
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": options,
        "keep_alive": "10m",
    }).encode()
    url = endpoint.rstrip("/") + "/api/generate"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        for line in r:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            chunk = obj.get("response", "")
            if chunk:
                yield chunk
            if obj.get("done"):
                return


def strip_fences(raw_iter: Iterator[str]) -> Iterator[str]:
    """Drop lone markdown code-fence lines (``` or ```lang) from the stream.

    Buffers chunk-by-chunk into lines; any line that consists only of a
    fence marker is dropped. Adds at most one line of latency.
    """
    buf = ""
    for chunk in raw_iter:
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if FENCE_LINE.fullmatch(line):
                continue
            yield line + "\n"
    if buf and not FENCE_LINE.fullmatch(buf):
        yield buf

def stream_levels(raw_iter: Iterator[str]) -> Iterator[tuple[int, str]]:
    """Yield (level, content_chunk) pairs. Delimiters suppressed.

    At each level boundary, strips leading whitespace AND any leading
    markdown horizontal rule (`---`, `***`, `===`, `___`) that some models
    emit as decoration between the LEVEL marker and the actual content.
    Rstrips trailing whitespace right before the next delimiter. Inter-line
    whitespace inside a level is preserved.
    """
    accum = ""
    current = 0  # 0 = preamble before LEVEL 1
    just_started = False
    for chunk in raw_iter:
        accum += chunk
        while True:
            m = LVL_DELIM.search(accum)
            if m is None:
                # Hold back ~30 chars in case a partial delimiter is forming
                safe = max(0, len(accum) - 30)
                if safe > 0 and current > 0:
                    out = accum[:safe]
                    if just_started:
                        out = _strip_level_lead(out)
                        if out:
                            just_started = False
                    if out:
                        yield current, out
                accum = accum[safe:]
                break
            before = accum[:m.start()]
            if current > 0 and before:
                out = before
                if just_started:
                    out = _strip_level_lead(out)
                out = out.rstrip()
                if out:
                    yield current, out
            current = int(m.group(1))
            just_started = True
            accum = accum[m.end():]
    # Final flush
    if current > 0 and accum:
        out = accum
        if just_started:
            out = _strip_level_lead(out)
        out = out.rstrip()
        if out:
            yield current, out


def _strip_level_lead(s: str) -> str:
    """Remove leading whitespace and any markdown horizontal-rule line that
    sometimes appears between the LEVEL marker and the actual content."""
    s = s.lstrip()
    m = LEADING_HR.match(s)
    if m:
        s = s[m.end():].lstrip()
    return s

# --- formatters --------------------------------------------------------------

class Formatter:
    def begin(self, levels: list[int]) -> None: ...
    def emit(self, level: int, chunk: str) -> None: ...
    def end(self) -> None: ...

class ColorFormatter(Formatter):
    """Suppresses delimiters; brightness gradient signals the levels.

    color-256 spreads the gradient evenly across the actual level count
    (so N=3 looks distinct from N=7), bounded from 255 (brightest) to 240
    (still readable dim gray) — never bottoms out at near-black.
    """
    GRAYSCALE_BRIGHT = 255  # near-white
    GRAYSCALE_DIM = 240     # readable dim gray; stays visible on dark terms

    def __init__(self, mode_256: bool):
        self.mode_256 = mode_256
        self.last_level = 0
        self.n_levels = 1

    def _color(self, level: int) -> str:
        if self.mode_256:
            if self.n_levels <= 1:
                return f"\033[38;5;{self.GRAYSCALE_BRIGHT}m"
            idx = min(level - 1, self.n_levels - 1)
            step = (self.GRAYSCALE_BRIGHT - self.GRAYSCALE_DIM) / (self.n_levels - 1)
            gray = round(self.GRAYSCALE_BRIGHT - idx * step)
            return f"\033[38;5;{gray}m"
        return {1: "\033[1m", 2: "\033[0m"}.get(level, "\033[2m")

    def begin(self, levels: list[int]) -> None:
        self.n_levels = len(levels)

    def emit(self, level: int, chunk: str) -> None:
        if level != self.last_level:
            if self.last_level > 0:
                sys.stdout.write("\033[0m\n\n")
            sys.stdout.write(self._color(level))
            self.last_level = level
        sys.stdout.write(chunk)
        sys.stdout.flush()

    def end(self) -> None:
        sys.stdout.write("\033[0m\n")
        sys.stdout.flush()

class JsonlFormatter(Formatter):
    """One JSON object per completed level."""
    def __init__(self) -> None:
        self.buffer: dict[int, str] = {}
        self.targets: list[int] = []

    def begin(self, levels: list[int]) -> None:
        self.targets = levels

    def emit(self, level: int, chunk: str) -> None:
        self.buffer.setdefault(level, "")
        self.buffer[level] += chunk

    def end(self) -> None:
        for level in sorted(self.buffer):
            content = self.buffer[level].strip()
            if not content:
                continue
            target = self.targets[level - 1] if 0 <= level - 1 < len(self.targets) else None
            actual = sum(count_units(content))
            obj = {
                "level": level,
                "target_words": target,
                "actual_words": actual,
                "content": content,
            }
            print(json.dumps(obj, ensure_ascii=False), flush=True)

# --- CLI ---------------------------------------------------------------------

def _positive_ratio(s: str) -> float:
    v = float(s)
    if v <= 1.0:
        raise argparse.ArgumentTypeError(f"ratio must be > 1.0, got {v}")
    return v


def _positive_int(s: str) -> int:
    v = int(s)
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {v}")
    return v


def _unit_fraction(s: str) -> float:
    v = float(s)
    if not (0.0 < v <= 1.0):
        raise argparse.ArgumentTypeError(f"must be in (0.0, 1.0], got {v}")
    return v


def _temperature(s: str) -> float:
    v = float(s)
    if not (0.0 <= v <= 2.0):
        raise argparse.ArgumentTypeError(f"must be in [0.0, 2.0], got {v}")
    return v


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mipmap",
        description="Progressive disclosure summarization. Streams a stack of "
                    "summaries at progressively larger sizes, smallest first. "
                    "Best on prose (articles, dialogues, AI responses, technical "
                    "writeups). Highly tabular sources (data lists, reference "
                    "tables) produce useful TLDRs but flat upper levels because "
                    "the model won't enumerate table rows during summarization.",
    )
    p.add_argument("file", nargs="?", help="input file (default: stdin)")
    p.add_argument("-m", "--model",
                   default=os.environ.get("MIPMAP_MODEL", DEFAULT_MODEL))
    p.add_argument("-e", "--endpoint",
                   default=os.environ.get("MIPMAP_ENDPOINT", DEFAULT_ENDPOINT))
    p.add_argument("-f", "--format",
                   default=os.environ.get("MIPMAP_FORMAT", "plain"),
                   choices=["plain", "color", "color-256", "jsonl"])
    p.add_argument("--floor", type=_positive_int,
                   default=(int(os.environ["MIPMAP_FLOOR"]) if os.environ.get("MIPMAP_FLOOR") else None),
                   help=f"smallest level's target size in units (words for "
                        f"Latin sources, characters for CJK); default "
                        f"{DEFAULT_FLOOR_LATIN} for Latin / {DEFAULT_FLOOR_CJK} "
                        f"for CJK. Larger values produce a denser TLDR with "
                        f"fewer mipmap levels overall.")
    p.add_argument("-c", "--compression", type=_unit_fraction,
                   default=float(os.environ.get("MIPMAP_COMPRESSION", DEFAULT_COMPRESSION)),
                   help=f"largest-level size as a fraction of source (0,1]; "
                        f"default {DEFAULT_COMPRESSION:g} (15%%)")
    p.add_argument("--ratio", type=_positive_ratio,
                   default=float(os.environ.get("MIPMAP_RATIO", DEFAULT_RATIO)),
                   help=f"growth factor between adjacent levels; must be > 1 "
                        f"(default {DEFAULT_RATIO}, classic mipmap doubling). "
                        f"Smaller values like 1.5 produce a finer ladder; "
                        f"larger like 3 jumps faster")
    p.add_argument("--max-levels", type=_positive_int,
                   default=int(os.environ.get("MIPMAP_MAX_LEVELS", DEFAULT_MAX_LEVELS)),
                   help="cap on auto-computed level count (default "
                        f"{DEFAULT_MAX_LEVELS}); ignored if --levels is set")
    p.add_argument("--levels", type=_positive_int, default=None,
                   help="force exactly N levels (must be > 0) by geometric "
                        "growth from --floor at --ratio (e.g. --levels 3 "
                        "with default floor=20 ratio=2 → [20, 40, 80]); "
                        "overrides --max-levels and the auto-compression cap")
    p.add_argument("-t", "--temperature", type=_temperature,
                   default=float(os.environ.get("MIPMAP_TEMP", DEFAULT_TEMP)),
                   help=f"sampling temperature in [0.0, 2.0] "
                        f"(default {DEFAULT_TEMP:g})")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--lang", default="auto", choices=["auto", "en", "zh"])
    p.add_argument("--max-chars", type=int,
                   default=(int(os.environ["MIPMAP_MAX_CHARS"])
                            if os.environ.get("MIPMAP_MAX_CHARS") else -1),
                   help="truncate input above this many chars; 0 disables; "
                        "default auto-scales with --num-ctx and detected language "
                        f"(~num_ctx*{LATIN_CHARS_PER_TOKEN:g} for Latin, "
                        f"~num_ctx*{CJK_CHARS_PER_TOKEN:g} for CJK)")
    p.add_argument("--num-ctx", type=int,
                   default=(int(os.environ["MIPMAP_NUM_CTX"])
                            if os.environ.get("MIPMAP_NUM_CTX") else -1),
                   help="ollama context window in tokens; default queries the "
                        "model's modelfile via /api/show, falling back to "
                        f"{FALLBACK_NUM_CTX} if the query fails")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print stderr diagnostic showing source size, level "
                        "targets, and chosen model")
    return p.parse_args(argv)

def read_input(path: str | None) -> str:
    if path is None:
        if sys.stdin.isatty():
            sys.stderr.write("mipmap: no input. Pipe text via stdin or pass a file path.\n")
            sys.exit(2)
        return sys.stdin.read()
    with open(path, encoding="utf-8") as f:
        return f.read()

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if os.environ.get("NO_COLOR") and args.format in ("color", "color-256"):
        args.format = "plain"

    src = read_input(args.file)
    if not src.strip():
        sys.stderr.write("mipmap: input is empty.\n")
        return 2

    cjk = is_cjk_dominant(src, args.lang)

    # Resolve num_ctx: query the model's modelfile if the user didn't specify.
    num_ctx_source = "explicit"
    if args.num_ctx < 0:
        detected = query_model_num_ctx(args.endpoint, args.model)
        if detected is not None:
            args.num_ctx = detected
            num_ctx_source = f"modelfile ({args.model})"
        else:
            args.num_ctx = FALLBACK_NUM_CTX
            num_ctx_source = "fallback"

    if args.max_chars < 0:
        # Auto-scale based on language and num_ctx.
        per_token = CJK_CHARS_PER_TOKEN if cjk else LATIN_CHARS_PER_TOKEN
        budget_tokens = max(1000, args.num_ctx - reserved_tokens_for(args.num_ctx))
        args.max_chars = int(budget_tokens * per_token)

    if args.max_chars > 0 and len(src) > args.max_chars:
        # Always warn — silent truncation is data loss the user should know about.
        sys.stderr.write(
            f"mipmap: input too long ({len(src)} chars), truncating to {args.max_chars}.\n"
        )
        src = src[:args.max_chars]
    a, c = count_units(src)
    units = a + c
    floor = args.floor if args.floor else (DEFAULT_FLOOR_CJK if cjk else DEFAULT_FLOOR_LATIN)
    if args.levels is not None:
        # Explicit override: geometric sequence from floor, exactly N entries.
        levels = [round(floor * (args.ratio ** i)) for i in range(args.levels)]
    else:
        levels = calibrated_levels(units, floor, args.compression,
                                    args.max_levels, args.ratio)

    if not levels or (len(levels) == 1 and levels[0] >= units):
        if args.verbose:
            sys.stderr.write("mipmap: input too short to summarize, passing through.\n")
        sys.stdout.write(src)
        if not src.endswith("\n"):
            sys.stdout.write("\n")
        return 0

    if args.verbose:
        unit_label = "字" if cjk else "words"
        levels_str = ", ".join(str(w) for w in levels)
        level_word = "level" if len(levels) == 1 else "levels"
        sys.stderr.write(
            f"mipmap: source {units} {unit_label}, computing {len(levels)} "
            f"{level_word}: {levels_str} "
            f"({args.model}, num_ctx={args.num_ctx} from {num_ctx_source})\n"
        )

    prompt = make_prompt(src, levels, cjk)

    try:
        raw = stream_raw(args.endpoint, args.model, prompt,
                          args.temperature, args.seed, args.num_ctx)
        defenced = strip_fences(raw)

        if args.format == "plain":
            for chunk in defenced:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            sys.stdout.write("\n")
        else:
            formatter: Formatter
            if args.format == "color":
                formatter = ColorFormatter(mode_256=False)
            elif args.format == "color-256":
                formatter = ColorFormatter(mode_256=True)
            else:
                formatter = JsonlFormatter()
            formatter.begin(levels)

            raw_buffer: list[str] = []
            def teeing() -> Iterator[str]:
                for chunk in defenced:
                    raw_buffer.append(chunk)
                    yield chunk

            emitted_any = False
            try:
                for level, chunk in stream_levels(teeing()):
                    formatter.emit(level, chunk)
                    emitted_any = True
            finally:
                formatter.end()

            if not emitted_any:
                sys.stderr.write(
                    "mipmap: warning — no levels parsed from model output, "
                    "emitting raw response instead.\n"
                )
                sys.stdout.write("".join(raw_buffer))
                if raw_buffer and not raw_buffer[-1].endswith("\n"):
                    sys.stdout.write("\n")
    except urllib.error.URLError as e:
        sys.stderr.write(f"mipmap: cannot reach ollama at {args.endpoint}: {e}\n")
        sys.stderr.write("mipmap: hint — is ollama running? `ollama serve`\n")
        return 1
    except KeyboardInterrupt:
        sys.stdout.write("\n\033[0m")
        sys.stdout.flush()
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
