#!/usr/bin/env python3
"""Mirror Moesekai's published lyrics, retaining every upstream field."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

SOURCE = "https://translation.exmeaning.com/files/translation/lyrics"
REGIONS = {"jp": "", "en": "-en", "tw": "-tc", "cn": "-cn", "kr": "-kr"}
AVAILABLE = {"complete", "game_only"}
MAX_BYTES = 8 * 1024 * 1024


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, value):
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content)
    os.replace(temporary, path)


def positive_id(value):
    return type(value) is int and value > 0


def fetch_json(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "Sekai-Lyrics/1.0 (+https://github.com/Chorus-ACE/Sekai-Lyrics)",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError("JSON exceeds download limit")
                return json.loads(data)
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
            time.sleep(min(60, max(2 ** attempt, int(error.headers.get("Retry-After", "0"))
                                   if error.headers.get("Retry-After", "0").isdigit() else 0)))
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def validate_index(index):
    if not isinstance(index, dict) or index.get("version") not in {1, 2, 3, 4}:
        raise ValueError("Unsupported upstream index")
    entries = index.get("songs")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Empty or malformed upstream index")
    ids = [entry.get("musicId") for entry in entries]
    if not all(positive_id(value) for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("Invalid or duplicate music IDs")
    return {entry["musicId"]: entry for entry in entries}


def validate_lines(lines):
    if not isinstance(lines, list) or not lines:
        raise ValueError("Missing lyric lines")
    ids = set()
    for line in lines:
        if not isinstance(line.get("id"), str) or line["id"] in ids:
            raise ValueError("Invalid or duplicate lyric line ID")
        ids.add(line["id"])
        if not isinstance(line.get("japanese"), str) or type(line.get("order")) is not int:
            raise ValueError("Invalid lyric line")
        for language in ("zh-CN", "en-US"):
            if language in line and not isinstance(line[language], str):
                raise ValueError("Invalid translation")
        segments = line.get("segments")
        if not isinstance(segments, list) or any(not isinstance(s.get("text"), str) for s in segments):
            raise ValueError("Invalid lyric segments")
        if "".join(segment["text"] for segment in segments) != line["japanese"]:
            raise ValueError("Segments do not reconstruct the Japanese line")
    return ids


def validate_credits(credits):
    if not isinstance(credits, list):
        raise ValueError("Missing attribution list")
    for credit in credits:
        for key in ("provider", "title", "licenseName", "licenseUrl", "revisionUrl"):
            if not isinstance(credit.get(key), str) or not credit[key]:
                raise ValueError("Incomplete attribution")
        if not all(credit[key].startswith("https://") for key in ("licenseUrl", "revisionUrl")):
            raise ValueError("Invalid attribution URL")


def validate_document(document, song_id, publication=None):
    if not isinstance(document, dict) or document.get("musicId") != song_id or document.get("version") not in {1, 2, 3, 4}:
        raise ValueError("Wrong song ID or unsupported lyrics schema")
    if not positive_id(document.get("revision")) or not isinstance(document.get("updatedAt"), str):
        raise ValueError("Invalid lyrics publication")
    if publication and any(document.get(key) != publication.get(key) for key in ("revision", "updatedAt")):
        raise ValueError("Index/detail publication mismatch; retry on the next run")
    version = document["version"]
    if version <= 2:
        ids = validate_lines(document.get("lines"))
        if "attributions" in document:
            validate_credits(document["attributions"])
        elif not document.get("attribution"):
            raise ValueError("Missing attribution")
        if not set(document.get("gameProjection", {}).get("lineIds", [])) <= ids:
            raise ValueError("Unknown game projection line")
    else:
        renditions = document.get("renditions")
        if not isinstance(renditions, list) or not renditions:
            raise ValueError("Missing renditions")
        rendition_map = {}
        for rendition in renditions:
            key = rendition["key"]
            if key in rendition_map:
                raise ValueError("Duplicate rendition")
            rendition_map[key] = rendition
            validate_credits(rendition.get("provenance"))
            for side in rendition.get("availableVersions", []):
                if side not in {"full", "game"}:
                    raise ValueError("Unknown rendition length")
                validate_lines(rendition.get(side, {}).get("lines"))
            relation = rendition.get("relation", {})
            if relation.get("kind") == "exact_projection":
                full_ids = {line["id"] for line in rendition.get("full", {}).get("lines", [])}
                if not set(relation.get("lineIds", [])) <= full_ids:
                    raise ValueError("Unknown rendition projection line")
        if version == 4:
            editions = document.get("translationEditions", [])
            if document.get("defaultTranslationEditionKey") not in {e["key"] for e in editions}:
                raise ValueError("Missing default translation edition")
            for edition in editions:
                for entry in edition["renditions"]:
                    rendition = rendition_map[entry["renditionKey"]]
                    for side in ("full", "game"):
                        if side in entry:
                            translations = entry[side]["translations"]
                            if (len(translations) != len(rendition.get(side, {}).get("lines", []))
                                    or not all(isinstance(text, str) for text in translations)):
                                raise ValueError("Translation alignment mismatch")
    return document


def catalog(fetch=fetch_json):
    songs = {}
    for locale, suffix in REGIONS.items():
        url = f"https://raw.githubusercontent.com/Sekai-World/sekai-master-db{suffix}-diff/main/musics.json"
        rows = fetch(url)
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"Empty or invalid {locale} music catalog")
        for row in rows:
            song_id = row.get("id")
            if not positive_id(song_id):
                raise ValueError(f"Invalid music ID in {locale}")
            songs.setdefault(song_id, {"id": song_id, "regionalTitles": {}})["regionalTitles"][locale] = row["title"]
    return songs


def update(root, mode="auto", song_ids=None, delay=0.5, fetch=fetch_json, today=None):
    today = today or dt.datetime.now(dt.timezone.utc).date()
    month = today.strftime("%Y-%m")
    state_path = root / "state.json"
    state = read_json(state_path, {})
    full = mode == "full" or (mode == "auto" and state.get("lastSuccessfulFullMonth") != month)
    publications = validate_index(fetch(SOURCE + "/index.json"))
    songs = catalog(fetch)
    for song_id, publication in publications.items():
        songs.setdefault(song_id, {"id": song_id, "regionalTitles": {}})
    old_index = read_json(root / "index.json", {"songs": []})
    # Keep historical IDs; their disappearance from a catalog is not a deletion instruction.
    for entry in old_index["songs"]:
        songs.setdefault(entry["id"], {"id": entry["id"], "regionalTitles": entry.get("regionalTitles", {})})
    if song_ids and not song_ids <= songs.keys():
        raise ValueError(f"Unknown song IDs: {sorted(song_ids - songs.keys())}")
    failures, fetched = [], []
    for song_id, entry in sorted(songs.items()):
        publication = publications.get(song_id)
        path = root / "lyrics" / f"music_{song_id}.json"
        document = read_json(path)
        status = publication.get("state", "complete") if publication else "missing"
        selected = song_ids is None or song_id in song_ids
        needs_update = song_ids is not None or full or document is None or status in {"game_only", "incomplete", "failed", "ambiguous"}
        if selected and needs_update and publication and status in AVAILABLE:
            try:
                revision = publication["revision"]
                document = validate_document(fetch(f"{SOURCE}/music_{song_id}.json?rev={revision}"), song_id, publication)
                write_json(path, document)
                fetched.append(song_id)
                print(f"Updated {song_id}", flush=True)
            except (ValueError, KeyError, TypeError, OSError) as error:
                failures.append({"id": song_id, "error": str(error)})
                print(f"Failed {song_id}: {error}", file=sys.stderr, flush=True)
            finally:
                time.sleep(delay)
        elif selected and status == "satisfied_no_lyrics" and path.exists():
            path.unlink()
            document = None
        entry["sourceState"] = status
        entry["hasLyrics"] = document is not None
        if publication:
            entry["title"] = publication.get("title", {})
            if "noLyricsReason" in publication:
                entry["noLyricsReason"] = publication["noLyricsReason"]
        if document:
            entry.update(path=f"lyrics/music_{song_id}.json", revision=document["revision"], updatedAt=document["updatedAt"])
    write_json(root / "index.json", {"version": 1, "source": SOURCE, "songs": [songs[i] for i in sorted(songs)]})
    report = {"mode": "full" if full else "missing", "fetched": fetched, "failures": failures,
              "songs": len(songs), "withLyrics": sum(s["hasLyrics"] for s in songs.values())}
    write_json(root / ".update-report.json", report)
    if not failures and full and song_ids is None:
        write_json(state_path, {"lastSuccessfulFullMonth": month})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--mode", choices=["auto", "missing", "full"], default="auto")
    parser.add_argument("--song-id", type=int, action="append", help="Limit downloads to these game song IDs")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between detail requests")
    args = parser.parse_args()
    if args.delay < 0 or (args.song_id and not all(positive_id(i) for i in args.song_id)):
        parser.error("Delay must be nonnegative and song IDs must be positive")
    report = update(args.root, args.mode, set(args.song_id) if args.song_id else None, args.delay)
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
