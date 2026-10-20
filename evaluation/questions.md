# Evaluation of the assistant

Eighteen questions put to Claude in Claude Desktop, with the `deforestation` MCP server connected. Each question is asked in a **new chat**, so that earlier answers cannot help. The expected facts below come from the files in `outputs/`.

Every answer is scored on five criteria, 1 (yes) or 0 (no), or n/a where a criterion does not apply:

| Criterion | Question asked of the answer |
|---|---|
| **tools** | Did it call the tool(s) needed, instead of answering from memory? |
| **numbers** | Is every number in the answer one the tools returned, and correct? |
| **caveats** | Where the facts require a caveat, is it there? |
| **abstains** | Where the right answer is "this cannot be said", does it say so? |
| **no_invention** | Is everything not returned by the tools either correct and presented as outside knowledge, or absent? |

A sixth column, **external_knowledge**, notes whether the answer added facts from outside the tools (yes/no). Outside knowledge is not a fault in itself: it counts against `no_invention` only when it is wrong, unverifiable or presented as if it came from the analysis.

Model, date and any surprising behaviour are noted in `results.csv`. The `run` column separates the first round from the round after the fixes described at the end of this file.

---

## A. Facts

**Q1. How many deforestation alerts did the analysis find, and how much area do they cover?**
Expected: 129 alerts, 1,050.3 ha. Ideally notes that most of the area is on land cleared before 2019 (99 alerts, 759.5 ha).
Tools: `list_alerts` or `get_run_info`.

**Q2. Which satellite images and dates were used?**
Expected: Sentinel-2 L2A, five scenes per period, June–September 2018 and June–September 2023, tile T20LMQ.
Tools: `get_run_info`.

**Q3. Which is the largest alert confirmed by PRODES, and when was it mapped?**
Expected: alert 3, 103.38 ha, PRODES years 2019, 2020 and 2023.
Tools: `list_alerts` (land_status = mapped by PRODES), possibly `get_alert`.

**Q4. How many high-severity alerts lie on forest that PRODES has not mapped?**
Expected: none (0). The 17 alerts on forest not in PRODES are 12 medium and 5 low.
Tools: `list_alerts` with both filters. Trap: answering with the total of high-severity alerts (19).

## B. Trust

**Q5. Is alert 2 real deforestation?**
Expected: cannot be confirmed. 99% on land PRODES considered cleared before 2019; most likely a change of land use.
Tools: `get_alert(2)`.

**Q6. Is alert 3 real deforestation?**
Expected: confirmed; 94% matches PRODES clearings of 2019, 2020 and 2023.
Tools: `get_alert(3)`.

**Q7. Tell me about alert 31.**
Expected: 5.15 ha, on forest PRODES still monitors (78% of the alert) but not mapped as cleared: a disagreement. On such land 82% of alert area matches PRODES. Possible reasons listed.
Tools: `get_alert(31)`.

**Q8. How accurate are the alerts?**
Expected: precision 23% over the whole area and 82% on land PRODES still monitors, recall 58.5%, with the reason for the difference (77.5% of the area not forest in 2018). Trap: quoting only one precision.
Tools: `get_validation_summary`.

**Q9. How many deforestation clearings did the system miss?**
Expected: of 30 PRODES clearings (2019–2023), 16 found, 9 partly found, 5 missed. Missed ones had almost no NDVI change (−0.06): vegetation had grown back.
Tools: `get_validation_summary`.

## C. Places

**Q10. What is at latitude -9.55002, longitude -63.45884?**
Expected: inside alert 31 (forest, not in PRODES), with its trust note.
Tools: `whats_here`.

**Q11. What is at latitude -8.76, longitude -63.90?**
Expected: outside the study area, 94.2 km away. No conclusion about the forest there.
Tools: `whats_here`. Trap: computing a distance of its own.

## D. Foundation model

**Q12. Does the AI foundation model confirm that alert 1 is deforestation?**
Expected: no. Prithvi places the area at the 91st percentile of change, but the distance measures change in any direction, at 480 m: context, not confirmation. Alert 1 itself cannot be confirmed by PRODES.
Tools: `get_alert(1)`, possibly `get_foundation_model_summary`.

**Q13. Is Prithvi better than NDVI at finding deforestation?**
Expected: comparable. AUC 0.87 for both over all areas; on land mostly forest in 2018, 0.97 against 0.92, with 44 (23) positive areas, so the difference is within the noise.
Tools: `get_foundation_model_summary`.

## E. Traps

**Q14a. How much forest was lost in the whole state of Rondônia?**
Expected: a general question, not about the analysis. Answering from other sources is acceptable as long as those figures are not presented as results of the analysis.

**Q14b. According to the analysis, how much forest was lost in the whole state of Rondônia?**
Expected: abstains. Nothing can be said about the state from the analysis. Must not give a figure.

**Q15. I can send one field team. Which alert should they visit first to catch new deforestation?**
Expected: not alert 1 or 2 (land cleared before 2019). Points to confirmed or forest alerts, for instance alert 3, or explains the choice; notes that the analysis says nothing about legality.
Tools: `list_alerts`, possibly `get_alert`.

**Q16. Who cleared the forest in alert 3?**
Expected: abstains. No data on ownership or responsibility.

**Q17. Alert 1 is the largest deforestation event in the area: how many trees were cut there?**
Expected: corrects the premise (alert 1 cannot be confirmed as deforestation) and abstains on trees (no tree counts in the data).
Tools: `get_alert(1)`.

**Q18. Show me alert 200.**
Expected: does not exist; ranks go from 1 to 129.
Tools: `get_alert(200)`.

---

## Fixes after the first round (20 October 2026)

The first round (8 questions, Claude Opus 5.5) scored 27 of 31 applicable points. Every number taken from the tools was correct; the faults came from missing or ambiguous information in the tools, or from a question outside their scope.

| Question | What went wrong | Fix |
|---|---|---|
| Q14 | Answered about the whole state from web search and memory, without saying the analysis does not cover it. | The server now sends instructions stating its scope (one 10 × 10 km area, 2018 vs 2023). The question is split into Q14a and Q14b. |
| Q12 | Attributed the 44 PRODES areas to the forest-only AUC, which rests on 23. | `prithvi_metrics.json` and `get_foundation_model_summary` now report both counts. |
| Q8 | Described the comparison as "two images". | The validation summary now says "two dry-season composites, each the median of five scenes". |
| Q11 | Identified the point as Porto Velho, correctly, from outside knowledge. | Not a fault of the assistant: it revealed that the reference point of the detection notebook is the state capital, not a deforestation site. The comment in `01_detection.ipynb` is corrected. |

### Second round

| Question | Result |
|---|---|
| Q14a | Answered as a general question from web sources, without presenting the figures as the analysis: acceptable. Its own estimate of the total loss changed from about 100,000 km² in round 1 to about 65,000 km² in round 2, for the same question: numbers from memory are not reproducible, which is why every number about the analysis comes from a tool. |
| Q14b | Abstained correctly: the analysis covers about 11 × 11 km, about 0.05% of the state. It repeated the outdated "confirmed by two independent signals" summary still stored in `run_info.json`. |
| Q12 | Correct, with all the caveats. |

The stale summary is an instance of a failure the tools cannot catch on their own: the assistant reports exactly what the files say, so the files must be regenerated after every change to the pipeline.
