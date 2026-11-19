Back to the [README](../README.md).

# Evaluation
A small eval suite lives in `eval/`: 108 questions (`eval/questions.json`; the 86 described here were run together, and the 22 buy-intent questions below were run separately). Expected facts were checked against the scraped
data first, and matching ignores case, accents, commas, spacing and `[n]` citation markers. The groups:
40 **answerable** questions with expected facts (23 fact lookups, 8 country and comparison, 6 paraphrases with little word
overlap such as "Who runs DarGlobal day to day?", 3 boundary questions that mention investing but are factual, e.g. "What do
ROI, ROE and ROL mean according to DarGlobal?"); 6 the data **cannot answer** (Wasalt's support phone, DarGlobal's share price,
an off-topic question); 24 **investment-advice** questions that must be refused; and 16 more factual questions that must simply
not be refused.

Live deployment, 2026-11-19, with the three-model chain:

| Metric | Score | What it measures |
|---|---|---|
| Retrieval hit rate | **38 / 40 (95%)** | retrieved context contains the expected facts (offline, no model) |
| Answer accuracy | **38 / 40 (95%)** | the model's answer contains the expected facts |
| Correct "I don't know" | **6 / 6 (100%)** | unanswerable questions answered with "I don't have that" |
| False "I don't know" | **1 / 40** | answerable questions answered with an "I don't have..." opening (see the note on "I don't know" detection below) |
| Investment advice refused | **24 / 24** | tuned-on 6/6, seen-while-tuning 10/10, **held-out 8/8** |
| False refusals | **0 / 62** | factual questions wrongly refused |
| Answers citing a passage `[n]` | **39 / 39** | 0 answers with a citation number that does not exist |
| Cited passages contain the facts | **37 / 38** | of fact-correct answers that cite: the cited passage really holds the fact |

By group, answers: fact lookups 22/23, country and comparison 8/8, paraphrases 5/6, boundary 3/3.

**The advice guardrail, honestly.** The regex layer alone refuses only the phrasings it was written for: offline it catches
6/6 of the questions it was tuned on but just 1/10 and 2/8 on two fresh sets (I tuned until the first fresh set passed, then
wrote the second set after freezing the design). The model layer is what generalizes: together they refused 24/24, including
all 8 held-out questions, with no false refusals. Eight held-out questions is a small sample, and the model layer relies on a
free model following the marker instruction.

**Lead handoff (buy-intent detection).** 14 phrasings that should show the contact card ("I want to buy a villa in Muscat",
"Could I come and see the show apartment?"), 6 written while building the regex and 8 fresh ones written afterwards, and
8 near-miss questions that must not ("What is DarGlobal's sales office address in Riyadh?"). Live, 2026-11-19:

| | Regex layer alone (offline) | Whole system (live) |
|---|---|---|
| Phrasings written while building the regex | 6 / 6 | 6 / 6 |
| **Held-out phrasings** | **0 / 8** | **5 / 8** |
| False cards on other questions | 0 / 94 | 0 / 24 (the near-misses plus 16 factual questions) |

The model layer is what generalizes, as with the advice guardrail, but it is not perfect: it missed *"put down a deposit on a
unit"*, *"sign up to get on the list"* and *"I'm ready to move forward with an apartment"*. I did not add patterns for these (they
would turn the held-out set into training data); a missed card costs little because the Enquire button is always there. Run just
this group with `python eval/run_eval.py --url <app> --only v6,v7,v8 --out eval/results_handoff.json`; results are in
`eval/results_handoff.json`.

**What hybrid retrieval and metadata changed** (retrieval hit rate on the same 37 questions, offline):

| | BM25 only | Hybrid + metadata |
|---|---|---|
| All questions | 32 / 37 (86%) | **35 / 37 (95%)** |
| Fact lookups | 22 / 23 | 22 / 23 |
| Country and comparison | 6 / 8 | 7 / 8 |
| Paraphrases | 4 / 6 | **6 / 6** |
| Comparison questions retrieving both companies | 0 / 3 | **3 / 3** |
| Retrieved chunks tagged with the asked country | 97% | 100% |

Honest reading: the embeddings alone are weak (18/23 on fact lookups, 2/6 on paraphrases; a larger 32M model was both worse
and too big for the instance), so the gain comes from blending them with BM25. Metadata is what makes comparisons work.
The first useful chunk ranks slightly lower with the hybrid (mean reciprocal rank 0.76 vs 0.78): coverage improved, ordering
did not. The new question groups were written before tuning, but 43 questions is a small set and the blend weight (0.6) was
picked between 0.5 and 0.8, not fine-tuned.

**Known failures:** *"Which brands partner with DarGlobal?"* (the brand list sits in a chunk with unrelated text and the page
says "partnered", not "partner"; the answer names only Lamborghini) and *"Who runs DarGlobal day to day?"* (it says the excerpts
don't say, though they name the CEO, so it is also the one false "I don't know"). *"Can a foreigner own a home in Riyadh?"*
failed in an earlier run, answering from DarGlobal's page on foreign buyers instead of Wasalt's (the question names no
company), and passed in this one: free-model answers vary between runs. Earlier experiments with smaller chunks and a suffix
stemmer gave no net gain on keyword retrieval alone.

Note on "I don't know" detection: the first live run flagged one answerable question (the Oman project list) as an "I don't know",
because it opened "I don't have a complete list..." and then listed real projects, so its sources were hidden. The detector
(`is_unknown` in `app/retrieval.py`) now only counts a denial in the *first sentence* and treats a partial admission
("complete", "full", "exhaustive", "comprehensive") as an answer, which keeps its sources. This changes how the answer is
classified, not what the model said: the same saved answers were re-scored (still 6/6 correct on unanswerable questions) and only that
one answer changed.

    python eval/run_eval.py --retrieval-only                      # free, no model calls
    python eval/run_eval.py --url https://<your-app>.onrender.com  # full run; --resume reuses cached answers

Caveats: the check is "expected facts are present", so it does not penalise extra or loosely worded content; the free model
is non-deterministic, so rerunning can shift individual results. Full per-question output is in `eval/results.json`. A full
run makes roughly 60 model requests, more than the free OpenRouter tier's ~50 a day, so it needs an account with credit.
