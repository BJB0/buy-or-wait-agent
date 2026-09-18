# Buy or Wait agent

## Run

```bash
python code/main.py
```

Writes `output.csv` at the repository root (250 rows).

Evaluate against public samples:

```bash
python code/evaluation/main.py
```

## Environment

Optional: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `XAI_API_KEY`. If none are set, evidence extraction uses the local cached image totals plus regex/rules on messages. No live banking or FX APIs.

Python 3.10+ standard library is sufficient. `pytesseract` and Pillow are optional OCR fallbacks.
