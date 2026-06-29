# MT5 Portfolio Analyzer

Reconstructs a combined multi-pair portfolio from separate MT5 pair backtests.

The analyzer merges deals and balance/equity curves from each pair, simulates one shared account balance, and dynamically rescales lot size as the combined balance changes.

## What It Does

- Ingests per-pair MT5 deals reports (closed PnL events).
- Ingests per-pair MT5 balance-equity curve exports (for floating PnL approximation).
- Replays all pair deals in one chronological stream on a single shared balance.
- Applies dynamic lot scaling factor at each event:

	scale factor = (combined balance / initial balance) ^ scale exponent

- Infers per-pair baseline EA configuration directly from MT5 backtests (single source of truth).
- Outputs combined balance/equity curve, event log, and performance summary.

## Repository Layout

- `src/mt5_portfolio_analyzer.py`: CLI and simulation engine.
- `config/example_config.json`: Example configuration.

## Input Files

### 1) Deals Report CSV (per pair)

Expected columns (header names are matched flexibly, case-insensitive):

- Time (required)
- Profit (required)
- Commission (optional)
- Swap (optional)
- Fee (optional)
- Volume / Lots (optional, used for baseline lot reference)

Net PnL per deal is computed as:

net = profit + commission + swap + fee

### 2) Balance-Equity Curve CSV (per pair)

Expected columns:

- Time (required)
- Balance (required)
- Equity (required)

Floating PnL at each timestamp is computed as:

floating = equity - balance

If no curve is provided for a pair, combined equity will follow balance-only behavior.

## Configuration

Use JSON config like `config/example_config.json`.

Key fields:

- `initial_balance`: starting combined account balance.
- `scale_exponent`: 1.0 for linear scaling, <1.0 for softer scaling, >1.0 for aggressive scaling.
- `min_scale`: lower clamp for scaling factor.
- `max_scale`: upper clamp for scaling factor.
- `pairs`: list of pair definitions:
	- `name`
	- `deals_file` / `xlsx_file` (required)
	- `curve_file` (optional)
	- `market_file` (optional)

Per-pair lot sizing is inferred from MT5 history and replayed as:

new_lot = round(combined_balance * inferred_risk_percent / 100000, 2)

The analyzer infers and stores (per pair):

- risk percent
- take profit (pips, when inferable)
- grid size (pips, when inferable)
- max concurrent trades
- initial balance
- first lot
- median lot
- trade count

## Usage

1. Put your CSV exports somewhere in the repository.
2. Update `config/example_config.json` paths and lot settings.
3. Run:

```powershell
python src/mt5_portfolio_analyzer.py --config config/example_config.json --output-dir output
```

## Outputs

The analyzer writes three files to `output-dir`:

- `combined_events.csv`: deal-by-deal replay with scaling and running balance.
- `combined_curve.csv`: combined time series with balance, floating PnL, equity.
- `summary.json`: final metrics and per-pair contribution.

## Notes On Approximation

- Closed PnL scaling assumes approximately linear PnL-to-lot behavior.
- Equity reconstruction uses each pair's historical floating profile sampled from its standalone backtest curve and scaled by the current portfolio scale factor.
- This is a practical portfolio reconstruction approximation, not a tick-level joint re-simulation.

## Example

The provided example config includes your four-pair setup:

- EURUSD risk 3.3
- EURGBP risk 1.2
- GBPUSD risk 2.0
- USDCHF risk 1.5