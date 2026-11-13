# Data sources

This repository holds code only. No data are included. Every input is a public table or a licensed extract that you download yourself.
Published tables are revised and re-released, so we record the release and the date we downloaded each file instead of copying it.

- `data_manifest.csv`: one row per raw input file we used (path, size, SHA-256, file date). Use it to check that a file you download is the one we used.
- `external_downloads.csv`: the download log written by `code/fetch_external.py` and earlier fetches (source, URL, UTC download time, size, SHA-256).

| Source | Producer | What we use | Release / version | Downloaded | How to get it |
|---|---|---|---|---|---|
| QCEW | BLS | Maryland county files, all ownerships, 2021 Q1–2024 Q4 | as published at download | 2026-10-10/11 | `code/fetch_qcew.py` (BLS QCEW open data API) |
| QWI | Census Bureau (LEHD) | Maryland, state and county, NAICS 4- and 6-digit, 2021–24 | R2026Q3, V4.14.0 | 2026-10-11 | `code/fetch_qwi.py` (LEHD latest_release) |
| BED | BLS | Business Employment Dynamics, Maryland series | as published at download | 2026-10-16 | `code/fetch_bed.py` (set `BLS_CONTACT_EMAIL` in your environment) |
| CBP | Census Bureau | County Business Patterns, 2021–23 | as published at download | 2026-10-15 | `code/fetch_external.py` |
| BDS | Census Bureau | Business Dynamics Statistics, 2023 release | 2023 | 2026-10-15 | `code/fetch_external.py` |
| LODES | Census Bureau (LEHD) | LODES8, Maryland OD/WAC/RAC and crosswalk | LODES8 | 2026-10-15 | `code/fetch_external.py` |
| O*NET, OEWS, AI exposure | O*NET Center; BLS; Eloundou et al. (2023); Felten et al. | occupation measures | see `external_downloads.csv` | 2026-10-15 | `code/fetch_external.py` |
| NAICS concordance | Census Bureau | 2017 to 2022 NAICS | 2022 | 2026-10-15 | Census NAICS concordance page |
| Tract–PUMA relationship files | Census Bureau | 2010 and 2020 | 2010, 2020 | 2026-10-15 | Census geography relationship files |
| ACS and CPS microdata | IPUMS USA and IPUMS CPS | extracts usa_00003/5/6, cps_00002 | see codebooks | 2026-10-16 | request your own extract at ipums.org (registration and terms apply; we cannot redistribute) |

IPUMS extracts are listed by name and checksum only. To reproduce them, request an extract with the variables in the codebook (`code/read_ipums.py` lists what is read) and cite IPUMS as its terms require.
