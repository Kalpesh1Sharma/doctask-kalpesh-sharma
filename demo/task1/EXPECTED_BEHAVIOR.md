# Expected Task 1 Behavior

Do not use this file as a promise of exact LLM wording or an exact Fact/Finding count. It documents the durable behavior expected from the final live-verified Task 1 sequence.

## INITIAL: Closing Disclosure

The first watched source is `baseline/closing_disclosure.txt`.

It contains grounded values including:

- borrower: Jordan Lee;
- property address: 18 Oak Ridge Lane, Austin, TX 78704;
- loan amount: USD 320,000;
- interest rate: 6.375%;
- term: 30 years;
- escrow: yes;
- cash to close: USD 19,250.

The configured cash-to-close rule has a USD 19,000 limit, so the INITIAL Run should surface a source-backed rule violation for USD 19,250 and stop at `REVIEW_REQUIRED`.

After the reviewer approves that finding, the normal worker path should complete COMMIT and produce a verified report.

## UPDATE: Loan Estimate arrival

After the INITIAL Run is completed, `baseline/loan_estimate.txt` arrives as a second watched document and creates an UPDATE Run linked to the completed baseline.

The two sources disagree on:

- interest rate: 6.375% vs 6.250%;
- cash to close: USD 19,250 vs USD 18,750.

Both disagreements should be surfaced as grounded contradictions with exact evidence from both source versions.

The Loan Estimate value USD 18,750 is **below** the configured USD 19,000 threshold, so it must not create a new cash-to-close rule violation.

The Run must still stop at `REVIEW_REQUIRED`; semantic output alone must never commit the UPDATE.

## Mixed item-level UPDATE review

The Loan Estimate repeats several facts already present in the committed baseline and changes two material values.

For the verified demo corpus:

- approve the new 6.250% interest-rate fact;
- approve the new USD 18,750 cash-to-close fact;
- reject duplicate borrower, property-address, loan-amount, loan-term, escrow, and file-reference fact proposals.

After all required decisions, the committed UPDATE should report `Focused update proof = Present` and preserve unaffected baseline content rather than rewriting it wholesale.

## Optional Underwriting Approval

`baseline/underwriting_approval.txt` is provided as an additional mortgage-role source, but it is not required for the primary two-document demo. Adding it later should create another focused UPDATE rather than modifying the already completed Runs in place.

## Optional hostile-source behavior

`update/closing_disclosure.txt` is a later replacement version of the same logical Closing Disclosure and contains explicit hostile instructions asking the system to auto-approve, skip review, reveal environment variables, and invent facts.

The demonstration succeeds only if that content remains untrusted source data. In particular:

- the Run still follows the normal workflow and human gate;
- no human decision is created by the document;
- there is no document-triggered direct COMMIT;
- the source cannot reveal environment variables or otherwise control the application.

## Second corpus

The second corpus is a different synthetic loan file. Its documents agree on the main values and its cash-to-close value is below the configured rule limit. It exists as an optional “works the second time” proof within the declared mortgage domain.
