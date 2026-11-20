# Third-Party Data Attribution

## BA-characters-internal-id

Character and school metadata used by BACC is derived from **BA-characters-internal-id**, maintained by **Agent-0808**, with original data sourced from **KivoWiki**.

- **Project:** https://github.com/Agent-0808/BA-characters-internal-id
- **Original data source:** https://kivo.wiki
- **Source license notice:** https://kivo.wiki/license
- **Data license:** Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)
- **License:** https://creativecommons.org/licenses/by-sa/4.0/
- **Data version:** See the source release tag recorded in the local catalog metadata.

The release zip is also kept unmodified in the `release/` folder of the data directory, next to the `LICENSE-DATA.txt` that upstream ships inside it; keep the two together when redistributing the data.

### Modifications

BACC processes the original CSV and JSON datasets into a local SQLite database (`ba_characters.db`), including schema normalization, searchable indexes, English school names, and additional translations of Spine remarks.

The adapted character catalog data is made available under CC BY-SA 4.0.

These acknowledgements do not imply that Agent-0808 or KivoWiki endorses BACC.

The data license applies to the adapted dataset, not automatically to BACC's independently developed application source code, which is released under the MIT License (see `LICENSE`).

The packaged glossary (`src/bacc/spine_remark_translate.json`) maps Spine remark terms found in that dataset to English; it is likewise an adaptation of the source data and is offered under CC BY-SA 4.0.
