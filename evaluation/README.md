# Evaluation

Generated 2026-11-28T08:40:13+00:00 by `python -m evaluation.evaluate`, from `outputs/*.json` compared with `answer_key/` (3 RFQs, 19 expected lines). Only this script reads the answer key; the pipeline never does.

## Headline

| Metric | Value | Meaning |
|---|---|---|
| **Extraction accuracy** | **89.5%** | Expected lines extracted with the right quantity and unit (17/19) |
| Match precision @ 0.8 | 100.0% | Auto-accepted lines (no review) with the right part (13/13) |
| Match recall @ 0.8 | 76.5% | Lines with an expected part, auto-accepted with the right one (13/17) |

## Extraction

| Metric | Value |
|---|---|
| Line recall (expected lines found) | 100.0% (19/19) |
| Line precision (extracted lines that are real items) | 100.0% (19/19) |
| Quantity accuracy (on found lines) | 100.0% (19/19) |
| Unit accuracy (on found lines, normalized) | 89.5% (17/19) |
| Customer identified | 100.0% |

## Matching

| Metric | Value |
|---|---|
| Top-1 part accuracy (right part, reviewed or not) | 100.0% (17/17) |
| Abstain accuracy (no part expected, none given) | 50.0% (1/2) |
| Review rate (found lines sent to a person) | 31.6% |

### Threshold sweep

A line is auto-accepted when its confidence is at least the threshold and it has no blocking flag.

| Threshold | Auto-accepted | Precision | Recall |
|---|---|---|---|
| 0.5 | 13 | 100.0% | 76.5% |
| 0.6 | 13 | 100.0% | 76.5% |
| 0.7 | 13 | 100.0% | 76.5% |
| 0.75 | 13 | 100.0% | 76.5% |
| 0.8 ← current | 13 | 100.0% | 76.5% |
| 0.85 | 13 | 100.0% | 76.5% |
| 0.9 | 13 | 100.0% | 76.5% |
| 0.95 | 8 | 100.0% | 47.1% |

## Per RFQ

| RFQ | Expected | Extracted | Found | Qty ✓ | Unit ✓ | Right part | Auto-accepted (right) |
|---|---|---|---|---|---|---|---|
| rfq_01_email | 5 | 5 | 5 | 5 | 3 | 4/4 | 2 (2) |
| rfq_02_clearwater | 6 | 6 | 6 | 6 | 6 | 6/6 | 6 (6) |
| rfq_03_tricounty | 8 | 8 | 8 | 8 | 8 | 7/7 | 5 (5) |

## Line by line

✓ right, ✗ wrong. Review = sent to a person.

| RFQ | # | As written (key) | Qty exp / got | Unit exp / got | Part exp / got | Method | Conf | Review | Flags |
|---|---|---|---|---|---|---|---|---|---|
| rfq_01_email | 1 | 20 ea 6204-2RS bearings | 20 / 20 ✓ | EA / EA ✓ | RL171482 / RL171482 ✓ | description | 0.9 | no | – |
| rfq_01_email | 2 | 10 A42 v-belts | 10 / 10 ✓ | EA / – ✗ | RL112676 / RL112676 ✓ | description | 0.9 | yes | unit_unclear |
| rfq_01_email | 3 | RL159429 x 3 | 3 / 3 ✓ | BX / – ✗ | RL159429 / RL159429 ✓ | ridgeline_part_number | 0.97 | yes | unit_unclear |
| rfq_01_email | 4 | 1/2-13 x 2" grade 8 hex bolts, 2 boxes | 2 / 2 ✓ | BX / box ✓ | RL165435 / RL165435 ✓ | description | 0.9 | no | – |
| rfq_01_email | 5 | Same grease cartridges as last order, 2 cases | 2 / 2 ✓ | CS / case ✓ | – / RL195561 ✗ | description | 0.6 | yes | ambiguous_match, refers_to_previous_order, vague_description |
| rfq_02_clearwater | 1 | APX-BB-6206ZZ | 12 / 12 ✓ | EA / EA ✓ | RL178907 / RL178907 ✓ | cross_reference | 0.95 | no | substitution_allowed |
| rfq_02_clearwater | 2 | APX-VB-B55 | 4 / 4 ✓ | EA / EA ✓ | RL125203 / RL125203 ✓ | cross_reference | 0.9 | no | unverified_cross_reference, substitution_allowed |
| rfq_02_clearwater | 3 | APX-HF-JIC08-90 | 25 / 25 ✓ | EA / EA ✓ | RL120374 / RL120374 ✓ | cross_reference | 0.9 | no | discontinued_replaced, substitution_allowed |
| rfq_02_clearwater | 4 | APX-FL-HE10 | 6 / 6 ✓ | EA / EA ✓ | RL194646 / RL194646 ✓ | description | 0.9 | no | cross_reference_missing, substitution_allowed |
| rfq_02_clearwater | 5 | APX-GL-A4-L | 12 / 12 ✓ | PR / PR ✓ | RL131850 / RL131850 ✓ | cross_reference | 0.95 | no | substitution_allowed |
| rfq_02_clearwater | 6 | APX-BV-SS100 | 3 / 3 ✓ | EA / EA ✓ | RL185180 / RL185180 ✓ | cross_reference | 0.95 | no | substitution_allowed |
| rfq_03_tricounty | 1 | NF-220418 3/4 brass ball valve FP | 15 / 15 ✓ | EA / EA ✓ | RL134741 / RL134741 ✓ | cross_reference | 0.95 | no | – |
| rfq_03_tricounty | 2 | NF-220420 1" brass ball valve FP | 10 / 10 ✓ | EA / EA ✓ | RL179507 / RL179507 ✓ | cross_reference | 0.95 | no | – |
| rfq_03_tricounty | 3 | same as above but 1/2" | 10 / 10 ✓ | EA / EA ✓ | RL172063 / RL172063 ✓ | description | 0.9 | yes | refers_to_other_line |
| rfq_03_tricounty | 4 | NF-118203 2" flange gasket 150# | 40 / 40 ✓ | EA / EA ✓ | RL118373 / RL118373 ✓ | cross_reference | 0.95 | no | – |
| rfq_03_tricounty | 5 | NF-118204 3" flange gasket 150# | 40 / 40 ✓ | EA / EA ✓ | RL124050 / RL124050 ✓ | cross_reference | 0.75 | yes | cross_reference_note |
| rfq_03_tricounty | 6 | NF-310077 AW46 hyd oil 5 gal | 6 / 6 ✓ | EA / PAIL ✓ | RL107944 / RL107944 ✓ | cross_reference | 0.95 | no | – |
| rfq_03_tricounty | 7 | NF-505512 20x20x2 pleated filter MERV 8 | 4 / 4 ✓ | CS / CS ✓ | RL149009 / RL149009 ✓ | cross_reference | 0.95 | no | – |
| rfq_03_tricounty | 8 | NF-999120 Chlorine residual test kit | 2 / 2 ✓ | EA / EA ✓ | – / – ✓ | – | – | yes | cross_reference_missing, no_match |

## RFQs without an answer key

Not scored. Shown so the review load is visible.

| RFQ | Lines | Matched | Need review | Customer |
|---|---|---|---|---|
| rfq_04_handwritten | 5 | 5 | 5 | C1004 |
| rfq_05_forwarded_chain | 3 | 3 | 1 | – |
| rfq_06_messy | 6 | 4 | 6 | C1008 |

## How to read this

- **Extraction accuracy** scores the LLM step: did each requested item come out, with the right quantity and unit? Units are normalized (`ea`, `each`, `EA` are the same).
- **Match precision** is the number that matters most ("flag, don't guess"): of the lines the system would let through without a person, how many have the right part.
- **Match recall** is how much work the system saves: the share of lines that are both right and auto-accepted. The rest go to review.
- Predicted lines are paired with expected lines by text similarity of `as_written`, one to one, so a renumbered line still counts (minimum similarity 60/100).
