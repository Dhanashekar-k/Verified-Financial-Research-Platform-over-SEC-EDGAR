"""LLM judges for the eval harness (M04 sections 3.7 and 3.8).

Three separate prompts, never merged (M04 pitfall): ``correctness`` compares an answer with the
gold answer and never sees evidence; ``support`` checks a claim against its cited evidence and
never sees gold; ``narrative`` scores gold-point coverage and contradiction. Only the
correctness judge is calibrated against Shekar's labels (Cohen's kappa, ``calibrate``); the
other two are reported as uncalibrated next to every number they produce.
"""
