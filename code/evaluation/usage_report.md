# Token usage report — Buy or Wait? final dataset run

## Run

- Command: `python code/main.py`
- Dataset: `dataset/requests.csv` (250 requests)
- Evidence: local deterministic extractors (image amount cache + message regex/rules)
- LLM/VLM APIs: not used (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `XAI_API_KEY` unset)

## Models

| Provider | Model | Calls | Input tokens | Output tokens | Cost (USD) |
|---|---|---|---:|---:|---:|
| none | local-rules + cached image totals | 0 | 0 | 0 | 0.00 |

## Totals

- Model calls: **0**
- Input tokens: **0**
- Output tokens: **0**
- Total tokens: **0**
- Average tokens / request: **0**
- Estimated total cost: **USD 0.00**
- Estimated cost / request: **USD 0.00**

Image amounts for the 16 blank `financial_events` rows were taken from the provided PNGs and cached under `code/agent/.cache/evidence.json` so the 250-row run stays repeatable without API spend. Message mutations (salary date/amount, rent increase, contract ended, pending payouts) are parsed with rules, never as authority over the 90-day cash-flow engine.
