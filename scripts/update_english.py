#!/usr/bin/env python3
"""Supplement mirrored lyrics with aligned English from their fixed Sekaipedia revision."""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
import threading
import urllib.error
import unicodedata
import urllib.parse
import urllib.request

from update_lyrics import validate_document, validate_license, write_json

API = "https://www.sekaipedia.org/w/api.php"
VERSION = 1
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST = 0.0
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class SourceIdentityMismatch(ValueError):
    """An upstream provenance reference points at another song; never import it."""


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def nodes(self, tag):
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == tag:
                    yield child
                yield from child.nodes(tag)

    def text(self):
        if self.tag in {"rt", "rp", "sup", "script", "style"}:
            return ""
        if self.tag == "br":
            return "\n"
        return "".join(child.text() if isinstance(child, Node) else re.sub(r"\s+", " ", child)
                       for child in self.children)


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", text))


def translation_tables(html):
    """Use explicit Japanese/English lyric columns, never infobox English titles."""
    result = []
    for table in Page(html).root.nodes("table"):
        if not table.attrs.get("id", "").startswith("lyrics-"):
            continue
        pairs, credits = [], []
        for row in table.nodes("tr"):
            cells = [child for child in row.children if isinstance(child, Node) and child.tag in {"td", "th"}]
            japanese = next((c for c in cells if "lyrics-japanese" in c.attrs.get("class", "").split()), None)
            english = next((c for c in cells if "lyrics-english" in c.attrs.get("class", "").split()), None)
            if japanese is not None and english is not None and japanese.tag == english.tag == "td":
                ja, en = japanese.text().split("\n"), english.text().split("\n")
                # A merged English sentence is not a line-aligned translation.
                if len(ja) != len(en):
                    continue
                for original, translation in zip(ja, en):
                    if normalized(original) and translation.strip():
                        pairs.append((normalized(original), translation.strip()))
            elif len(cells) == 1:
                credit = " ".join(cells[0].text().split())
                if re.search(r"English translation", credit, re.I):
                    credits.append(credit)
        if pairs:
            result.append((pairs, credits))
    return result


def aligned_translation(lines, tables):
    wanted = [normalized(line["japanese"]) for line in lines]
    candidates = []
    for pairs, credits in tables:
        # Prefer the exact rendition/length. Only then allow an unambiguous
        # full-to-game projection, identified by actual Japanese text.
        if [ja for ja, _ in pairs] == wanted:
            candidates.append((0, [en for _, en in pairs], credits))
            continue
        by_text = {}
        for ja, en in pairs:
            by_text.setdefault(ja, set()).add(en)
        if all(ja in by_text and len(by_text[ja]) == 1 for ja in wanted):
            candidates.append((1, [next(iter(by_text[ja])) for ja in wanted], credits))
    if not candidates:
        return None
    best = min(item[0] for item in candidates)
    chosen = [item for item in candidates if item[0] == best]
    if len({tuple(item[1]) for item in chosen}) != 1:
        return None
    return chosen[0][1], list(dict.fromkeys(credit for item in chosen for credit in item[2]))


def fetch_page(source, cache):
    revision = source["revisionId"]
    if type(revision) is not int or revision <= 0:
        raise ValueError("Invalid source revision")
    parsed = urllib.parse.urlsplit(source["revisionUrl"])
    if parsed.scheme != "https" or parsed.hostname != "www.sekaipedia.org" or parsed.username:
        raise ValueError("Unexpected Sekaipedia source URL")
    if urllib.parse.parse_qs(parsed.query).get("oldid") != [str(revision)]:
        raise ValueError("Source URL and revision disagree")
    path = cache / f"sekaipedia-{revision}.json"
    if path.exists():
        response = json.loads(path.read_text())
    else:
        def request_json(params):
            request = urllib.request.Request(API + "?" + urllib.parse.urlencode(params), headers={
                "User-Agent": "Mozilla/5.0", "Accept": "application/json",
                "Referer": "https://www.sekaipedia.org/"})
            for attempt in range(3):
                global _LAST_REQUEST
                with _REQUEST_LOCK:
                    time.sleep(max(0, 0.5 - (time.monotonic() - _LAST_REQUEST)))
                    _LAST_REQUEST = time.monotonic()
                try:
                    with urllib.request.urlopen(request, timeout=30) as received:
                        data = received.read(8 * 1024 * 1024 + 1)
                    break
                except urllib.error.HTTPError as error:
                    if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                        raise
                    time.sleep(2 ** (attempt + 1))
                except (urllib.error.URLError, TimeoutError):
                    if attempt == 2:
                        raise
                    time.sleep(2 ** (attempt + 1))
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("Wiki response exceeds download limit")
            return json.loads(data)
        # The public API rejects combined parse props; fetch the rendered table
        # and restriction markers separately, both pinned to the same revision.
        response = request_json({"action": "parse", "oldid": revision, "prop": "text",
                                 "format": "json", "formatversion": 2})
        raw = request_json({"action": "query", "prop": "revisions", "revids": revision,
                            "rvprop": "content", "rvslots": "main", "format": "json", "formatversion": 2})
        pages = raw.get("query", {}).get("pages", [])
        if len(pages) != 1 or pages[0].get("title") != response.get("parse", {}).get("title"):
            raise ValueError("Wiki source identity mismatch")
        response["parse"]["wikitext"] = pages[0]["revisions"][0]["slots"]["main"]["content"]
    page = response.get("parse", {})
    if page.get("revid") != revision:
        raise SourceIdentityMismatch("Wiki revision mismatch")
    # Provenance sometimes differs only in capitalization of romanized titles.
    if normalized(page.get("title", "").replace("_", " ")).casefold() != normalized(source["title"].replace("_", " ")).casefold():
        raise SourceIdentityMismatch(f"Wiki title mismatch: {source['title']} points at {page.get('title')}")
    raw = page.get("wikitext", "")
    if isinstance(raw, dict):
        raw = raw.get("*", "")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Missing source restriction metadata")
    html = page.get("text", "")
    if isinstance(html, dict):
        html = html.get("*", "")
    if not isinstance(html, str) or not html.strip():
        raise ValueError("Missing rendered lyric source")
    if not path.exists():
        write_json(path, response)
    if re.search(r"\{\{\s*(?:NoReprint|NoCopyright|CopyrightedLyrics)\b", raw, re.I):
        return []
    return translation_tables(html)


def enrich(document, fetch):
    result = copy.deepcopy(document)
    count = 0
    for rendition in result.get("renditions", []):
        credits = []
        sources = [s for s in rendition.get("provenance", []) if s.get("provider") == "sekaipedia"]
        sources = list({(s["revisionUrl"], s["revisionId"]): s for s in sources}.values())
        for side in ("full", "game"):
            lines = rendition.get(side, {}).get("lines", [])
            if not lines or all(line.get("en-US", "").strip() for line in lines):
                continue
            matches = []
            for source in sources:
                validate_license(source)
                match = aligned_translation(lines, fetch(source))
                if match:
                    matches.append((source, match))
            if not matches or len({tuple(match[0]) for _, match in matches}) != 1:
                continue
            source, (translations, side_credits) = matches[0]
            for line, translation in zip(lines, translations):
                if not line.get("en-US", "").strip():
                    line["en-US"] = translation
                    count += 1
            provenance = copy.deepcopy(source)
            provenance["component"] = f"renditions/{rendition['key']}/{side}_english_translation"
            if provenance not in rendition["provenance"]:
                rendition["provenance"].append(provenance)
            credits.extend(side_credits)
        if credits:
            rendition["englishTranslationCredits"] = {"translation": " ".join(dict.fromkeys(credits))}
    if count:
        result["englishEnrichmentVersion"] = VERSION
    validate_document(result, result["musicId"])
    return result, count


def update(root, song_ids=None, fetch=None):
    cache = root / ".cache" / "english-sources"
    memo = {}
    paths = sorted((root / "lyrics").glob("music_*.json"))
    if fetch is None:
        sources = {}
        for path in paths:
            document = json.loads(path.read_text())
            if song_ids and document["musicId"] not in song_ids:
                continue
            for rendition in document.get("renditions", []):
                if not any(not line.get("en-US", "").strip() for side in ("full", "game")
                           for line in rendition.get(side, {}).get("lines", [])):
                    continue
                for source in rendition.get("provenance", []):
                    if source.get("provider") == "sekaipedia":
                        validate_license(source)
                        sources[(source["revisionUrl"], source["revisionId"])] = source
        # A global start-time limiter caps all four workers at two API requests
        # per second. Independent page latency does not serialize the refresh.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fetch_page, source, cache): key for key, source in sources.items()}
            for index, future in enumerate(as_completed(futures), 1):
                key = futures[future]
                try:
                    memo[key] = future.result()
                except (ValueError, KeyError, TypeError, OSError) as error:
                    memo[key] = error
                if index % 20 == 0 or index == len(futures):
                    print(f"English sources: {index}/{len(futures)}", flush=True)
    def cached(source):
        key = (source["revisionUrl"], source["revisionId"])
        if key not in memo:
            memo[key] = fetch(source) if fetch else fetch_page(source, cache)
        if isinstance(memo[key], Exception):
            raise memo[key]
        return memo[key]
    report = {"updated": [], "englishLinesAdded": 0, "failures": [], "skipped": []}
    for path in paths:
        document = json.loads(path.read_text())
        if song_ids and document["musicId"] not in song_ids:
            continue
        try:
            enriched, count = enrich(document, cached)
            if enriched != document:
                write_json(path, enriched)
                report["updated"].append(document["musicId"])
                report["englishLinesAdded"] += count
                print(f"English {document['musicId']}: +{count} lines", flush=True)
        except SourceIdentityMismatch as error:
            report["skipped"].append({"id": document["musicId"], "reason": str(error)})
            print(f"English {document['musicId']}: skipped {error}", flush=True)
        except (ValueError, KeyError, TypeError, OSError) as error:
            report["failures"].append({"id": document["musicId"], "error": str(error)})
            print(f"English {document['musicId']}: {error}", flush=True)
    write_json(root / ".english-update-report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--song-id", type=int, action="append")
    args = parser.parse_args()
    report = update(args.root, set(args.song_id) if args.song_id else None)
    print(json.dumps(report))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
