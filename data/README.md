# data/ (gitignored)

Everything in this directory except this file is excluded from git — see the privacy
wall at the top of the repo's `.gitignore`.

Expected local layout:

```
data/
├─ raw/
│  ├─ personal/          # lab PDFs, cycle tracking, activity, nutrition exports
│  │  ├─ lab/
│  │  ├─ cycle_tracking/
│  │  ├─ activity/
│  │  ├─ nutrition/
│  │  └─ apple_health/   # export.xml backfill + Health Auto Export increments
│  └─ knowledgebase/     # proprietary corpus (copyrighted ebooks — never published)
│     ├─ hormones/
│     └─ nutrition/
├─ public_corpus/        # open PMOS/ESHRE/NICE guidelines, PubMed abstracts (reproducible benchmark)
├─ demo/                 # synthetic demo persona — the only data that appears in demos
└─ clean/                # normalized outputs (DuckDB / graph inputs)
```

The public benchmark runs on `public_corpus/`; results on the private corpus are
reported as aggregate numbers only. All screenshots and demos use `demo/`.
