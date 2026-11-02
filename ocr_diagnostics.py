#!/usr/bin/env python3
"""
OCR quality diagnostics: extract suspicious/misrecognized Japanese terms
from the 217 page OCR results without modifying anything.

Outputs (in ocr_diagnostics/):
  ngram_candidates.txt   - ranked suspicious n-grams: candidate, freq, pages, context samples
  variant_clusters.txt   - clusters of word variants with same outer chars (e.g. 可惑性/可変性/可六性)
  rare_chars.txt         - low-frequency CJK chars, top n-grams containing them
  page_stats.txt         - per-page char count, blank pages, long-line stats
  samples.json           - candidate -> list of {page, context} for later use
"""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).parent / "ocr"
OUT = Path(__file__).parent / "ocr_diagnostics"
OUT.mkdir(exist_ok=True)

CJK = re.compile(r"[\u3040-\u30ff\u3100-\u313f\u31f0-\u32ff\u4e00-\u9fff\u3400-\u4dbf]")
PUNCT = re.compile(r"[、。；：（）\[\]「」『』「」！？\s\-_.／/、]" )


def load_pages():
    pages = {}
    for f in sorted(BASE.glob("page-*.txt")):
        pages[int(f.stem.split("-")[1])] = f.read_text(encoding="utf-8")
    return pages


def clean_lines(text):
    # strip leading whitespace + line continuation bars used by this scan
    lines = []
    for ln in text.splitlines():
        s = re.sub(r"^[\s\|]", "", ln)
        if s:
            lines.append(s)
    return lines


def ngrams(text, n):
    # only CJK runs, skip across non-CJK
    chars = [c for c in text if CJK.match(c)]
    return ["".join(chars[i:i + n]) for i in range(len(chars) - n + 1) if len(chars) - n + 1 > i]


def contexts_of(needle, page, before=10, after=10):
    seen = []
    s = 0
    while True:
        i = page.find(needle, s)
        if i < 0:
            return seen
        seen.append(page[max(0, i - before):i + len(needle) + after])
        s = i + 1
    return seen


def main():
    pages = load_pages()
    n = max(pages)
    print(f"pages loaded: {len(pages)} (1..{n})")

    # per-char frequency
    char_freq = Counter(c for p in pages.values() for c in p if CJK.match(c))
    total = sum(char_freq.values())

    # n-gram global frequency (2,3)
    ng2 = Counter()
    ng3 = Counter()
    for p, text in pages.items():
        chars = [c for c in text if CJK.match(c)]
        for i in range(len(chars) - 2):
            ng2[tuple(chars[i:i + 3])] += 1
        for i in range(len(chars) - 1):
            ng2[tuple(chars[i:i + 2])] += 1
    # fix: keep separate
    ng2 = Counter()
    ng3 = Counter()
    for p, text in pages.items():
        chars = [c for c in text if CJK.match(c)]
        for i in range(len(chars) - 1):
            ng2[tuple(chars[i:i + 2])] += 1
        for i in range(len(chars) - 2):
            ng3[tuple(chars[i:i + 3])] += 1

    # per-page occurrence of each ngram
    occ3 = defaultdict(set)
    for p, text in pages.items():
        chars = [c for c in text if CJK.match(c)]
        for i in range(len(chars) - 2):
            occ3[tuple(chars[i:i + 3])].add(p)
        for i in range(len(chars) - 1):
            occ3[tuple(chars[i:i + 2])].add(p)

    def suspicious_score(g, n):
        # score: frequency high but rare-char content
        f = (ng3 if n == 3 else ng2)[g]
        score = 0.0
        for c in g:
            rf = char_freq[c] / total
            # rare char boosts score
            if rf < 0.001:
                score += 2.0
            elif rf < 0.005:
                score += 1.0
        # frequency boost
        score += min(f / 20.0, 2.0)
        # common-char penalty for obvious common words
        if all(char_freq[c] / total > 0.005 for c in g):
            score -= 1.0
        return score, f

    lines = []
    seen = set()
    for g, f in sorted(ng3.items(), key=lambda x: -x[1]):
        if f < 3:
            continue
        sc, _ = suspicious_score(g, 3)
        if sc <= 0.5:
            continue
        key = "".join(g)
        if key in seen:
            continue
        seen.add(key)
        pg = sorted(occ3.get(g, []))
        pages_s = ",".join(map(str, pg[:8])) + ("..." if len(pg) > 8 else "")
        lines.append((key, f, sc, len(pg), pages_s))

    # sort final by frequency, then score
    lines.sort(key=lambda x: (-x[1], -x[2]))
    top = lines[:120]

    with open(OUT / "ngram_candidates.txt", "w") as fh:
        fh.write("candidate\tfreq\tscore\tpages\tpagelist\n")
        for key, f, sc, np_, pages_s in top:
            fh.write(f"{key}\t{f}\t{sc:.2f}\t{np_}\t{pages_s}\n")

    # rare chars
    rare = sorted(((c, n) for c, n in char_freq.items() if n < total * 0.0003), reverse=False)
    rare = [(c, n) for c, n in char_freq.items() if n < total * 0.0003]
    rare.sort(key=lambda x: -x[1])
    rare_top = rare[:60]  # most frequent among the rare set
    with open(OUT / "rare_chars.txt", "w") as fh:
        fh.write(f"# rare CJK chars (freq < 0.03% of {total} chars)\n")
        fh.write(f"{'char':3} {'count':6} {'top bigrams':40}\n")
        for c, n in rare_top:
            bg = Counter()
            for p, text in pages.items():
                chars = [ch for ch in text if CJK.match(ch)]
                for i in range(len(chars) - 1):
                    if chars[i] == c:
                        bg[chars[i + 1]] += 1
                    if i > 0 and chars[i - 1] == c:
                        bg[chars[i - 1]] += 1
            top_bg = " ".join(f"{k}({v})" for k, v in bg.most_common(5))
            fh.write(f"{c}3 {n:5d}  {top_bg}\n")

    # variant clusters: same outer chars, varying middle
    clusters = defaultdict(dict)  # (outer pattern) -> {variant: freq}
    for g, f in list(ng2.items()):
        if f < 2:
            continue
        a, b = g[0], g[1]
        # find all 3-grams aXb
        pass
    for g, f in list(ng3.items()):
        if f < 2:
            continue
        a, m, b = g[0], g[1], g[2]
        # pattern aXb where a==b or fixed outer
        # we look for clusters where first char fixed and last char fixed, middle varies
        clusters[(a, b, len(g))]["".join(g)] = f

    # better: for each pair of (first,last), collect variants
    fl_variants = defaultdict(Counter)
    for g, f in ng3.items():
        fl_variants[(g[0], g[2])]["".join(g)] += f

    lines2 = []
    for (a, b), v in fl_variants.items():
        variants = [(w, f) for w, f in v.items() if f >= 3]
        if len(variants) >= 2:
            lines2.append(((a, b), sum(f for _, f in variants), variants))
    lines2.sort(key=lambda x: -x[1])

    with open(OUT / "variant_clusters.txt", "w") as fh:
        fh.write("# variant clusters: same outer chars, differing middle, total freq >= 6\n")
        fh.write(f"{'outer':8} {'total':6}\tvariants\n")
        for (a, b), t, variants in lines2[:80]:
            var_s = " | ".join(f"{w}({f})" for w, f in sorted(variants, key=lambda x: -x[1]))
            fh.write(f"{a}*{b}4 {t:5d}\t{var_s}\n")

    # page stats
    with open(OUT / "page_stats.txt", "w") as fh:
        stats = []
        for p in range(1, n + 1):
            t = pages[p]
            c = len([c for c in t if CJK.match(c)])
            stats.append((p, c))
        stats.sort(key=lambda x: -x[1])
        fh.write("# per-page CJK char counts (descending top 40 + bottom 40)\n")
        for p, c in stats[:40] + stats[-40:]:
            fh.write(f"page {p:3d}  {c:5d} chars\n")

    # samples.json for candidate -> contexts (top 50 by score)
    samples = {}
    for key, f, sc, np_, _ in top[:50]:
        ctx = []
        for p in sorted(occ3.get(tuple(key))):
            for c in contexts_of(key, pages[p]):
                ctx.append({"page": p, "context": c})
                if len(ctx) >= 6:
                    break
            if len(ctx) >= 6:
                break
        samples[key] = ctx
    with open(OUT / "samples.json", "w", encoding="utf-8") as fh:
        json.dump(samples, fh, ensure_ascii=False, indent=2)

    print("diagnostics written to", OUT)


if __name__ == "__main__":
    main()
