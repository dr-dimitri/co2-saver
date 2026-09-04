# CO2 Saver for Home Assistant

## Mission

Build and maintain a focused Home Assistant custom integration that calculates the CO2 savings achieved when self-generated photovoltaic energy replaces grid energy. Every change must measurably advance this integration.

## Non-negotiable focus

- Work only on the next unblocked project issue or on a prerequisite that is required to complete it.
- Do not add adjacent energy-management, cost-optimization, forecasting, dashboard, or device-control features unless an accepted issue explicitly brings them into scope.
- Record every unresolved product, measurement, or accounting decision in a GitHub issue. Do not silently choose a materially different product behavior.
- Keep implementation issues in dependency order. Do not start a dependent issue before its predecessor's acceptance criteria are met.
- Re-run the focus check below at the start of a task, before widening a change, and before declaring completion.

## Focus check

Answer all four questions before continuing:

1. Which project issue and acceptance criterion does this work satisfy?
2. Is this the smallest coherent change that moves that issue forward?
3. Does it preserve the CO2-accounting invariants below?
4. Have new open questions or follow-up work been captured in an issue?

If any answer is unclear, stop expanding the implementation and resolve or record the gap first.

## Product scope and accounting invariants

- Accept Home Assistant energy entities representing PV generation through an inverter or smart meter, household consumption, and optionally a battery storage system.
- Support either an aggregate household-consumption signal with configurable shares for additional consumers such as a wallbox, or separately measured household and additional-consumer signals.
- Keep those input modes explicit and mutually understandable; never count the same energy twice.
- Attribute direct PV savings only to PV energy consumed by configured loads rather than exported energy.
- Do not recognize battery-related savings when the battery charges. Track eligible stored PV energy and recognize its benefit only when that energy is discharged to a configured load.
- Prevent double counting between direct PV consumption and battery discharge.
- Apply user-configurable lifecycle CO2 factors in `gCO2e/kWh` for PV generation and battery throughput according to the accepted accounting model.
- Keep energy, power, and emissions units explicit. Treat meter resets, unavailable entities, counter rollovers, negative values, and incomplete intervals deliberately.
- Prefer conservative results when input data cannot support an exact attribution, and expose data-quality limitations rather than inventing precision.

## Repository skills

- Use `.agents/skills/co2-accounting/SKILL.md` for changes to formulas, energy attribution, storage state, emissions factors, or result semantics.
- Use `.agents/skills/home-assistant-integration/SKILL.md` for integration structure, config flows, entities, persistence, tests, or Home Assistant compatibility.
- When both areas are touched, use both skills and reconcile their constraints before editing code.

## Engineering workflow

- Read the active issue, its predecessor, and the relevant repository skills before implementation.
- Keep requirements, implementation, tests, and user documentation aligned in the same change.
- Prefer Home Assistant-native patterns and public APIs. Avoid unnecessary dependencies.
- Add tests for calculation boundaries, restart/restore behavior, unavailable inputs, resets, and every supported input topology before calling a feature complete.
- Run the narrowest relevant checks while iterating and the full project test and lint suite before completion.
- Do not weaken or delete tests to make a change pass without documenting and justifying the behavioral change.
- Preserve unrelated user changes in the working tree.

## Issue and completion discipline

- Each issue must state its predecessor, scope, acceptance criteria, and explicitly excluded follow-ups.
- New uncertainty belongs in an issue linked into the dependency chain before dependent implementation proceeds.
- A task is complete only when its acceptance criteria, tests, documentation, and focus check all pass.
