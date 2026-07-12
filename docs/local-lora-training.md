# Local LoRA Training For Crypto Snapshots

This project already captures the right raw material for a useful local model:

- structured market features
- signal/scoring/risk context
- liquidation and behavior context
- actual trade/no-trade decisions
- resolved forward labels from triple-barrier outcomes

That is the correct place to apply LoRA.

## What To Train

Train the model to classify the next move from a structured snapshot:

- `LONG`
- `SHORT`
- `FLAT`

Use one horizon at a time first:

- `15m`
- later `30m`

Do not ask the model to directly output order size, leverage, or liquidation math. Keep those deterministic in the engine.

## Export The Dataset

```bash
liquidity-signal dataset-summary BTCUSDT
liquidity-signal export-lora --symbol BTCUSDT --horizon-minutes 15 --limit 2000 --balance-mode undersample_majority
```

Files created:

- `runtime/lora_exports/btcusdt_15m_undersample_majority_manifest.json`
- `runtime/lora_exports/btcusdt_15m_undersample_majority_chatml.jsonl`
- `runtime/lora_exports/btcusdt_15m_undersample_majority_prompt_completion.jsonl`

API alternative:

```bash
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:8000/dataset/training/lora?symbol=BTCUSDT&horizon_minutes=15&limit=2000"
```

## Recommended Local Base Models

For laptop-friendly LoRA work, prefer:

- `Qwen/Qwen2.5-3B-Instruct`
- `Qwen/Qwen2.5-1.5B-Instruct`
- `TinyLlama/TinyLlama-1.1B-Chat-v1.0` only for very lightweight experiments

If you want stronger reasoning later and have more memory:

- `Qwen/Qwen2.5-7B-Instruct`
- `Llama-3.1-8B-Instruct`

## Training Objective

Primary target:

- predict the resolved label direction from the structured snapshot

Optional secondary target later:

- predict whether the setup should be traded or skipped

## Important Rules

- keep execution and risk deterministic
- use the model as a pattern detector and confidence layer
- train only on resolved labels
- monitor class balance; crypto datasets often overproduce `FLAT` or one-sided trend labels
- use balanced export for the first LoRA pass, then compare against raw export later
- split by snapshot id/time, not random row duplication
- evaluate by horizon and by symbol

## Practical Loop

1. Run the bot and market engine long enough to accumulate resolved snapshots.
2. Export a 15-minute labeled dataset.
3. Export a balanced version first if the raw labels are skewed.
4. Fine-tune a small instruct model with LoRA on the snapshot-to-label task.
5. Keep the raw manifest so you can compare balanced vs unbalanced training later.
6. Validate on held-out examples.
7. Use the trained adapter as a secondary reviewer, not the execution engine itself.

## Run Local Training

Install the training extras:

```bash
pip install -e '.[train]'
```

Run training:

```bash
liquidity-signal train-lora --config-path runtime/lora_training_config.example.json
```

## Run MLX LoRA On Apple Silicon

Install MLX-LM training support:

```bash
pip install -e '.[mlx]'
```

Prepare MLX chat-format splits:

```bash
liquidity-signal prepare-mlx-lora --horizon-minutes 5
liquidity-signal prepare-mlx-lora --horizon-minutes 15
```

Train the first 5-minute adapter:

```bash
python -m mlx_lm.lora \
  --model Qwen/Qwen2.5-1.5B-Instruct \
  --train \
  --data runtime/mlx_lora_data/crypto_5m_undersample_majority \
  --adapter-path runtime/mlx_lora_runs/qwen25-15b-crypto-5m \
  --iters 600 \
  --batch-size 1 \
  --grad-accumulation-steps 8 \
  --num-layers 8 \
  --mask-prompt
```

Evaluate it:

```bash
python -m mlx_lm.lora \
  --model Qwen/Qwen2.5-1.5B-Instruct \
  --adapter-path runtime/mlx_lora_runs/qwen25-15b-crypto-5m \
  --data runtime/mlx_lora_data/crypto_5m_undersample_majority \
  --test
```

## Why This Works Better Than Raw Price Fine-Tuning

The engine already computes the hard market structure:

- regime
- volatility
- liquidation pressure
- positioning
- funding/basis
- volume and flow
- risk framing

LoRA is then learning pattern relationships between those engineered signals and the future resolved outcome. That is much more sample-efficient than asking a small local model to rediscover market microstructure from raw OHLCV alone.
