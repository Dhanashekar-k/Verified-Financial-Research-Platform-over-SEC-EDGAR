# Results

Every number on this page comes from `eval/results/*.json` and is written by `eval/report.py`
between the AUTO markers. Nothing inside the markers is typed by hand. Each metric shows n and a
95% bootstrap CI (10,000 resamples). System-vs-system differences, when reported, use a
paired bootstrap.

**Tuning discipline:** systems are tuned on the **dev** split only; **test** is run at
milestones. FinanceBench accuracy is judged by an LLM, and the judge's agreement with human
labels (Cohen's kappa, `eval/judges/`) is printed next to it. The narrative judge is
uncalibrated and labelled as such.

<!-- AUTO:START -->
_No results files yet: every metric is NOT MEASURED._
<!-- AUTO:END -->
