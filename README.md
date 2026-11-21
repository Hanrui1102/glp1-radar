# GLP-1 Evidence Radar
Browse metabolic drug research by ingredient or clinical question. The standalone dashboard pairs PubMed publications with ClinicalTrials.gov registrations, highlights changes observed over the past seven days and includes three source-linked safety readings.

## Open the dashboard
Open `reports/index.html` in a browser. The snapshot works offline. Source links open the publication or registry entry. The weekly feed shows up to ten recent updates from retained history, relative to the last collection. Each source's first import is listed separately.

## Refresh
Run from the project folder with Python 3.10 or later. The collector uses the standard library.

```sh
python3 radar.py
python3 -m unittest discover -s tests
```

Set NCBI_EMAIL to your contact email when collecting from PubMed. NCBI_API_KEY is optional. Configure these as repository secrets for GitHub Actions. The workflow **Refresh research evidence** runs daily at 11:17 UTC and supports manual runs. Enable Actions and grant the workflow write access to persist the archive. Branch protections may restrict automatic commits.

The collector updates `data/catalog.json`, `reports/index.html` and `reports/latest.md`. Failed sources retain their archived records and the job reports a failure. To preview synthetic archive entries separately, run `python3 radar.py --demo` and open `outputs/demo/index.html`.

## Coverage
Edit `config/watchlist.json` to change ingredients, aliases and topic keywords. PubMed searches titles and abstracts over a rolling 45-day entry window. ClinicalTrials.gov returns the most recently updated matching interventions. The default cap is 500 records per source per run. Search on the dashboard uses archived titles and assigned ingredients. Topic labels come from titles and trial intervention names. Records outside a later query remain archived.

Changes describe metadata observed between collections. Registry completion and posted results have separate fields. The archive keeps up to 3,000 change events. Geographic locations identify trial sites. Regulatory label comparisons are planned. The three editorial safety cards are fixed readings and link to the original abstracts.

Code is MIT licensed. External data remain subject to the terms of their sources.

## Organizations and U.S. access
Research organizations includes dated sponsor disclosures, a university study listing and live institution-specific PubMed searches. Prices and insurance provides dated manufacturer self-pay examples and a U.S. coverage checklist. The daily collector refreshes journal and registry metadata. Editorial links and price examples require manual review.
