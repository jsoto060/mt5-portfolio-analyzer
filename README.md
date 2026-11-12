# MT5 Portfolio Analyzer

Reconstructs a combined multi-pair portfolio from separate MT5 pair backtests.

The analyzer merges deals and balance/equity curves from each pair, simulates one shared account balance, and dynamically rescales lot size as the combined balance changes.

## What It Does

- Ingests per-pair MT5 deals reports (closed PnL events).
- Ingests per-pair MT5 balance-equity curve exports (for floating PnL approximation).
- Replays all pair deals in one chronological stream on a single shared balance.
- Calculates an unrounded sizing coefficient for each original opening deal:

	entry risk coefficient = original lot * 100000 / original balance before entry

- Reconstructs each entry lot using the shared portfolio balance at that entry:

	new lot = round(original lot * portfolio balance before entry / original balance before entry, 2)

- Keeps the reconstructed lot fixed after entry for realized PnL, floating PnL, margin, and modeled swap.

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
- `scale_exponent`, `min_scale`, `max_scale`: legacy compatibility fields; not applied to per-entry lot sizing.
- `pairs`: list of pair definitions:
	- `name`
	- `deals_file` / `xlsx_file` (required)
	- `curve_file` (optional)
	- `market_file` (optional)

Per-entry lot sizing is inferred from MT5 history and replayed as:

new_lot = round(original_lot * combined_balance / original_entry_balance, 2)

The original entry balance starts at the report's initial deposit and follows MT5
deal-row sequence, including entry commissions, realized profit, and reported swap.
An entry uses the balance before its own costs are charged. Original entry balances
are retained when scenario filtering removes trades.

At the same original and reconstructed balance, each entry keeps its original lot.
This preserves recorded ATR/grid lot progression without selecting a sizing mode.

An existing `risk_percent` scenario override proportionally adjusts every entry's
coefficient by `scenario risk / baseline median risk`, preserving relative grid sizes.
The baseline median is rounded to one decimal for reporting only; individual entry
coefficients are not rounded. A risk override requires a positive baseline median.

The analyzer infers and stores (per pair):

- median entry sizing coefficient (reported as risk percent, not a stop-loss-based loss percentage)
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

When modeled swap is enabled, replay exports also include explicit ledger fields:

- `EventType`: one of `deal_in`, `deal_out`, `swap`
- `ModeledSwap`: per-event modeled swap impact (0 for non-swap rows)
- `CumulativeModeledSwap`: running total of modeled swap
- `BalanceAfterEvent`: balance immediately after the event is applied

## Deterministic Replay Event Ordering

Replay ordering is explicit and does not depend on filesystem discovery order.

Implementation points:

- Deal/trade events are created in `load_pair` in `src/mt5_portfolio_analyzer.py`.
- Per-pair events are merged in `PortfolioSimulator._build_sorted_deal_events` and `PortfolioSimulator._build_sorted_trade_events` in `src/mt5_portfolio_analyzer.py`.
- Sorting is performed with explicit keys (not insertion order):
	- timestamp ascending
	- pair alphabetical
	- original MT5 sequence within the pair
	- event type rank for ledger ties:
		- `swap` rank 0
		- `deal` rank 1

## Replay Ledger Contract

Balance replay is modeled as an explicit event ledger. Every balance-changing
operation must be represented as an event row. Hidden or implicit balance
adjustments are not allowed.

Balance-changing event categories:

- `deal_in`: entry-side deal row (includes modeled commission impacts when present in MT5 net values)
- `deal_out`: exit-side deal row
- `swap`: modeled daily swap event

This contract keeps accounting auditable and deterministic, and prevents future
changes from introducing implicit balance mutations outside the replay ledger.

## Swap Config Notes

`config/swap_rates.yaml` supports per-symbol `buy` and `sell` daily rates via:

- `daily_swap_per_001_lot`

Optional reserved fields are parsed for forward compatibility and currently
ignored by replay behavior:

- `effective_from`
- `effective_to`

Rationale:

- MT5 provides sequence inside each pair stream.
- MT5 does not define canonical ordering across different pairs when events share the same timestamp.
- Pair-name ordering is therefore a deterministic implementation convention used to make replay results stable across operating systems, filesystems, and file discovery order.

## Notes On Approximation

- Closed PnL scaling assumes approximately linear PnL-to-lot behavior.
- Equity and margin reconstruction use market prices and the same fixed entry lots as the balance ledger.
- Closing deals first match a same-side open position with the same remaining original volume. When a match is ambiguous, the newest matching position is used; otherwise, volume is allocated newest-first. Partial closes retain the original entry scale for the remaining position and modeled swap.
- Volume matching is a heuristic because the reader does not retain an MT5 position identifier. Equal-volume positions cannot always be uniquely matched to their original entries.
- Per-entry coefficients replay recorded EA decisions; they do not rerun ATR calculations, lot caps, minimum-lot rules, or equity-based/nonlinear sizing under changed conditions.
- This is a practical portfolio reconstruction approximation, not a tick-level joint re-simulation.

## Example

The provided example config includes your four-pair setup:

- EURUSD risk 3.3
- EURGBP risk 1.2
- GBPUSD risk 2.0
- USDCHF risk 1.5