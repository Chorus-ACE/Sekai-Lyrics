# Sekai Lyrics

Song-ID-addressed lyrics for SekaiKit and GreatSekai. This mirrors the **published JSON** used by [Moesekai](https://pjsk.moe/zh-cn/lyrics/788/), from `https://translation.exmeaning.com/files/translation/lyrics`. It does not crawl website pages. Requests are sequential, rate limited, and retried with backoff.

## Contents

- `lyrics/music_<Song.id>.json`: the complete upstream document, with all original fields preserved, including Japanese text, Chinese translation editions, stanza breaks, performer segments, ruby readings, full/game renditions, credits, component provenance, source revision links, and license links. The English supplement described below adds `en-US` lines and attributed source components; it does not replace upstream Japanese or Chinese text.
- `index.json`: the union of the five regional master-data song catalogs, upstream lyrics index, and previously known song IDs. `regionalTitles` uses SekaiLocale region codes (`jp`, `en`, `tw`, `cn`, `kr`). Translation languages describe text, not game regions. `sourceState` and `hasLyrics` distinguish unavailable/instrumental songs from downloadable lyrics. `revision` describes the locally mirrored document.
- `state.json`: last successfully completed full-refresh UTC calendar month.

An absent translation is **not** generated or relabeled. The upstream producer currently imports source lyrics without English and materializes Chinese translations only; consequently, the original mirror alone cannot supply English. Older schemas contain inline `zh-CN` / `en-US` translations. Upstream v4 translation editions currently contain Chinese only, as documented by the site's public client. Segments are the source's performer/text spans, not inferred linguistic tokenization.

## English supplement

`scripts/update_english.py` reads Sekaipedia revisions already identified by each
rendition's structured provenance. The public MediaWiki API supplies rendered
Japanese/English lyric columns and the same revision's wikitext. Ruby readings,
romaji, singer labels, footnotes, and English song titles are excluded. Multiline
cells must have matching line counts. Exact rendition/length matches take
precedence; game projections are accepted only when each Japanese line has one
unambiguous English translation. Incomplete or conflicting mappings remain absent.
No translation is generated, and existing upstream English is retained.

Each supplemented side gains a `full_english_translation` or
`game_english_translation` provenance component with the original provider,
fixed revision URL, and validated license. Table translation credits are retained
as `englishTranslationCredits`; source-only and Chinese translation credits remain
separate. `englishEnrichmentVersion` marks supplemented documents without changing
the upstream publication revision. Explicit no-reprint/copyright restriction
markers are skipped. Documents from other providers remain unchanged by this
supplement. A failed download preserves the last good document and fails the run
before publication. Invalid upstream provenance that points at a different song
is reported under `skipped` without importing it or blocking other valid songs.

Fixed-revision responses are cached under `.cache/english-sources` and reused by
Actions. Four bounded workers share a global limit of two API request starts per
second; transient failures retry with backoff. The daily workflow runs the English supplement after updating the mirror,
so refreshed Japanese/Chinese documents are enriched again before publication.
The two update reports are uploaded together. To refresh a sample:

```sh
python3 scripts/update_english.py --song-id 2
python3 scripts/update_english.py  # all mirrored documents
```

## Update

Requires Python 3.10+, with no third-party dependencies:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/update_lyrics.py                 # daily missing; full if this month has not succeeded
python3 scripts/update_lyrics.py --mode full    # refresh every available song
python3 scripts/update_lyrics.py --mode missing
python3 scripts/update_lyrics.py --song-id 788  # fetch by Song ID; does not mark full month complete
```

The workflow runs daily at 03:23 UTC (13:23 Brisbane). Each run reads all five regional `musics.json` catalogs and the public lyrics index. Missing records and game-only records are revisited daily; complete records are fetched monthly. Songs not yet published in the lyrics index are checked again on the next daily index read. A failed full refresh is retried on the following run, and the workflow publishes only after the entire run succeeds. Individual failed downloads never overwrite previous good documents. Unsupported schemas, wrong song IDs, inconsistent revisions, invalid segmentation, missing attribution, and misaligned translations fail validation. No-change runs create no commit.

Before writing any lyric document, the updater checks the license name **and** canonical HTTPS URL against this allowlist:

| License | Accepted URL |
| --- | --- |
| CC BY-SA 3.0 | `https://creativecommons.org/licenses/by-sa/3.0/` |
| CC BY-SA 4.0 | `https://creativecommons.org/licenses/by-sa/4.0/` |
| CC BY-NC-SA 3.0 | `https://creativecommons.org/licenses/by-nc-sa/3.0/` |

Every rendition must have a nonempty structured attribution list, and every component's license must match an allowed name/URL pair. Additional nested license records are checked too. An unapproved, missing, or mismatched license causes the **entire song document** to be skipped. Legacy free-text attribution alone cannot establish an allowed license. Approved existing revisions are retained when a new revision is skipped. Policy skips are logged and recorded separately under `skipped` in the Actions update-report artifact; they do not fail the run or prevent other songs from updating. New skipped songs remain missing and are checked again daily.

The upstream index contains no license metadata, so the updater must read the detail JSON to verify its licenses. Rejected detail documents are not written to the repository. This check applies to the supplied source metadata and does not infer licenses for text without its own explicit license record.

GitHub's scheduled runs may be delayed. Scheduled workflows in inactive public repositories may be disabled by GitHub after 60 days; [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) describes these platform limits. `workflow_dispatch` can run any mode manually. No external token is needed: publishing uses this repository's scoped `GITHUB_TOKEN`.

## Attribution and licensing

Data is **not relicensed under the code license**. Every mirrored document retains its original attribution and component-specific `licenseName`, `licenseUrl`, `revisionUrl`, and translator/proofreader credits. Sources can have different terms, including CC BY-SA 3.0, CC BY-SA 4.0, and CC BY-NC-SA 3.0. Consult each component's metadata; do not assume one repository-wide data license. Mirroring, JSON formatting, and explicitly attributed English supplementation are the changes made here. Rights in the underlying musical works remain with their respective owners.

The updater is independently implemented from the published data contract. Its code and tests are licensed under [MIT](LICENSE).
