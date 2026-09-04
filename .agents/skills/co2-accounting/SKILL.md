---
name: co2-accounting
description: "Design, implement, or review CO₂ accounting for this Home Assistant integration, including on-site PV self-consumption, household and optional consumer attribution, storage, lifecycle factors, persistence, and tests. Use for energy-flow models and sensor calculations; do not use for unrelated Home Assistant scaffolding or UI work."
---

# CO₂ accounting

Keep every calculation traceable to measured energy and an explicit measurement topology. Before changing requirements, code, or tests, determine which sensors are power versus cumulative energy, their units and sign conventions, and whether each consumer is already included in another meter. If a material semantic is unknown, record it as an open, prerequisite issue instead of inventing a silent default.

## Preserve the energy balance

- Normalize interval energy to `kWh` and emission intensity to `gCO₂e/kWh`. Convert power readings to energy with elapsed time; never treat `W` or `kW` as energy. Keep full precision internally and round only presentation values.
- Give every physical energy flow one owner. Derive direct PV use from measured flows or a documented balance, bounded by both PV supply and eligible load. PV export is not self-consumption.
- Support the two consumer topologies explicitly. An included consumer, such as a wallbox inside the household meter, may receive a measured or configured share of that same total; splitting it must not increase total consumption. Add a separately metered consumer only when the household reading excludes it. Never infer inclusion from entity names.
- Require allocated shares to be non-negative and to sum to at most the measured aggregate. Reconcile direct household use, attributed consumers, storage charge, grid import, and export within an explicit noise tolerance.
- Enforce conservation for every interval: attributed direct PV plus PV sent to storage plus PV export cannot exceed available PV, and attributed consumption cannot exceed measured consumption. Do not turn inconsistent or unavailable inputs into fictitious savings.

## Account for storage when energy is used

- Charging a battery creates no CO₂ saving. Credit eligible energy only when the battery discharges to a measured load, using the avoided grid intensity at discharge time.
- Maintain a persistent PV-origin inventory for storage. Increase it only from measured PV-attributed charge and reduce it for eligible discharge and losses. Bound it by usable capacity and never allow it below zero.
- If the battery can also charge from the grid, track provenance. Use a documented allocation policy, preferably proportional/weighted-average attribution, unless the product requirements choose another. Never classify all discharge as PV merely because PV was available earlier.
- Apply measured charge/discharge values and the chosen efficiency convention consistently. Losses cannot become direct use, stored inventory, or avoided grid energy.
- Do not count the same PV kWh both as direct use at charging time and again at battery discharge. Across all paths, credited PV energy remains bounded by the corresponding generation ledger.

## Calculate transparent emissions

Expose or retain separate components for gross avoided grid emissions, PV lifecycle burden, storage lifecycle burden, and net saving. A negative net result is valid; do not clamp it to zero.

For interval energy `E` and grid intensity `G`:

- Direct PV gross avoidance is `E_direct * G_use`.
- Eligible storage gross avoidance is `E_discharge_pv * G_discharge`; never use the grid intensity from charging time.
- Subtract the configurable, non-negative PV and storage manufacturing factors exactly once on their declared energy bases.

Make each factor's basis part of the entity/configuration contract. If the PV factor is per generated kWh, apply it to the PV generation associated with the delivered energy, including charge losses, and defer that burden with the battery inventory until discharge. If a factor is defined per delivered kWh instead, apply it to eligible delivered energy. State whether the storage factor is per charged, discharged, or throughput kWh; do not mix these bases. Keep `gCO₂e`, `kgCO₂e`, and energy conversions explicit.

## Remain correct across time

- Treat a decrease in a cumulative source meter as a reset or replacement: re-baseline it and emit no negative interval. Do not erase the integration's accumulated result.
- Persist ledger state and cumulative CO₂ totals across Home Assistant restarts. Processing the same sample twice must not add it twice. Derived `total_increasing` sensors must remain monotonic even when an input resets.
- Define behavior for stale, unavailable, out-of-order, duplicated, or long-gap samples. Prefer skipping an unprovable interval and reporting diagnostics over estimating unbounded energy.
- Apply changed factors and topology settings prospectively unless an explicit, reproducible historical recalculation is designed. Never silently rewrite prior totals.

## Resolve and test the accounting contract

Before implementation depends on them, capture unresolved choices in ordered issues: supported meter topologies; import/export and charge/discharge sign semantics; power integration cadence; aggregate-versus-separate consumer inclusion; fractional allocation rules; mixed-source battery policy; efficiency and lifecycle-factor bases; grid-intensity source and timing; noise tolerance; missing-data handling; meter resets; restart restoration; and configuration changes.

Test observable invariants, not wording. Cover direct PV use, export-only intervals, included and separate consumers, fractional allocation, a complete PV charge/discharge cycle, mixed grid/PV charging, losses, simultaneous flows, zero energy, negative net emissions, unit conversion, sensor unavailability, meter reset, duplicate/out-of-order samples, restart recovery, and prospective factor changes. Include property-style cases that assert conservation, non-negative inventories, monotonic cumulative totals, and absence of double counting.
