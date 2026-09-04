---
name: home-assistant-integration
description: Develop and review this repository's Home Assistant custom integration using current official architecture, config-flow, entity, persistence, statistics, testing, and documentation conventions. Use for implementation, refactoring, or review under custom_components; do not use for ordinary Home Assistant user configuration.
---

# Home Assistant Integration

Build a maintainable custom integration that behaves like a native Home Assistant integration while staying within the current issue's scope. Treat official Home Assistant developer documentation as the source of truth; repository conventions and the supported Home Assistant version determine how those rules are applied locally.

## Establish the context

Before changing or reviewing code:

- Read the repository instructions, the active issue and its dependencies, the manifest, config-entry lifecycle, platforms, strings/translations, tests, and user documentation relevant to the change.
- Identify whether the integration represents a device/service, a helper, or computed/virtual data before choosing architecture or `integration_type`.
- Determine the supported Home Assistant versions and existing test/lint/type-check commands. Do not introduce an API merely because it is present in the newest Core if the project supports older releases.
- Turn the issue into observable acceptance criteria. Raise unresolved product or domain semantics explicitly instead of embedding an undocumented assumption.

Recheck the active issue after inspecting the code and before finishing. Avoid unrelated cleanup, speculative features, packaging work, or abstractions that do not advance those criteria.

## Use current official guidance

Consult only the pages relevant to the work, and revisit them when behavior or APIs may have changed:

- [Integration architecture](https://developers.home-assistant.io/docs/architecture_components/) and [entity/device architecture](https://developers.home-assistant.io/docs/architecture/devices-and-services/)
- [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) and its linked rule pages
- [Config flow](https://developers.home-assistant.io/docs/core/integration/config_flow/) and [integration manifest](https://developers.home-assistant.io/docs/creating_integration_manifest/)
- [Entity](https://developers.home-assistant.io/docs/core/entity/) and [sensor entity](https://developers.home-assistant.io/docs/core/entity/sensor/) conventions
- [Testing Home Assistant code](https://developers.home-assistant.io/docs/development_testing/)
- [Integration documentation structure](https://developers.home-assistant.io/docs/documenting/integration-docs-examples/) and linked documentation standards

Use Bronze quality rules as the baseline checklist for new integration work. Apply higher-tier rules only when the repository declares that target or they directly improve the requested change. Verify a rule in code, tests, and documentation before calling it complete; record a justified exemption rather than treating it as implemented. Do not imply that a custom integration has been reviewed or certified by Home Assistant.

## Architecture and lifecycle

- Prefer config-entry-driven, fully asynchronous setup and unload. Keep setup/unload idempotent, forward platforms through supported APIs, release listeners/resources on unload, and make reload behavior testable.
- Keep domain logic separate from Home Assistant lifecycle and entity presentation so calculations and edge cases can be tested without bootstrapping all of Home Assistant.
- Share per-entry runtime state through the current supported config-entry pattern. Do not rely on untyped global dictionaries when a typed entry/runtime model is available for the supported version.
- Keep entity properties side-effect free and free of I/O. Fetch or derive data in async callbacks/update paths, then expose cached values.
- Register callbacks through Home Assistant lifecycle helpers such as `async_on_remove` where applicable. Ensure unloading or reloading an entry cannot leave duplicate subscriptions.
- Model `unknown`, `unavailable`, partial input, unit changes, source removal, and startup ordering deliberately. Never convert missing or invalid data into a plausible zero unless zero is semantically proven.
- For integrations that consume entities supplied by other integrations, follow the official helper/computed-data architecture. Validate the source entity's current semantics instead of assuming an entity ID always retains the same unit or device class, and do not claim ownership of another integration's device.
- Version config-entry schemas and provide migrations when persisted structure changes. Keep migration deterministic and test both successful and rejected/unsupported versions.

## Config flow and configuration

- Provide UI setup through `config_flow.py` and declare `config_flow: true` in the manifest. Use supported selectors and translated labels/errors instead of asking users for internal values when a dedicated selector exists.
- Validate required entities, supported domains/device classes/units, numeric bounds, and incompatible combinations before creating or updating an entry. Return actionable field or base errors without logging expected user mistakes as exceptions.
- Prevent duplicates with a stable unique identity when the integration has one. Do not use changeable network addresses, entity names, or display labels as identity.
- Separate stable setup data from user-adjustable options according to their lifecycle. Add options, reconfigure, reauth, or discovery flows only when the integration's behavior requires them.
- Keep secrets out of logs, diagnostics, titles, unique IDs, and entity attributes.
- Test every implemented flow branch through its public `FlowResult`: initial form, valid creation, validation errors, duplicate aborts, and any options/reconfigure/reauth paths.

## Entity conventions

- Use the platform-specific entity class and an entity-description pattern when it reduces duplication. Give every registry entity a deterministic, stable `unique_id`; use `has_entity_name`, translation keys, device information, and entity categories according to current official guidance.
- Expose separate entities for independently useful values. Keep extra state attributes small, stable, and genuinely supplementary.
- Use native values and Home Assistant unit constants. Set `device_class`, `native_unit_of_measurement`, and `state_class` only when the value's semantics satisfy their contracts.
- Distinguish instantaneous measurements from cumulative totals. Use `TOTAL` or `TOTAL_INCREASING` only after defining reset, decrease, correction, and monotonicity behavior; do not select a state class merely to make a graph appear.
- Preserve numeric precision internally and round only at a documented presentation boundary. Avoid float equality assumptions in calculations and tests.
- Represent invalid or unavailable upstream data as unavailable/unknown behavior consistent with Home Assistant, not as stale data presented as current.

## Restore, recorder, and statistics

- Decide explicitly which state must survive restart and what authoritative input reconstructs it. Restoration must not double-count events that occurred before shutdown or during startup.
- For sensor-native state restoration, use the current sensor-specific restoration API (for example `RestoreSensor` and `async_get_last_sensor_data` where supported), not generic `RestoreEntity` state parsing.
- Restore only compatible values and units. Handle missing, malformed, legacy, and migrated restore data safely, and register live updates in an order that avoids losing or counting startup events twice.
- Opt sensors into long-term statistics only with correct device class, unit, and state-class semantics. Verify restart, reset/decrease, and unit-change behavior in tests.
- Do not query or write recorder internals directly. If historical/statistical access is truly required, use the documented recorder/statistics API without blocking the event loop and test behavior when recorder data is absent.
- Exclude volatile, high-cardinality, or non-historical attributes using the documented class-level recorder exclusions when such attributes cannot be removed or modeled as entities.

## Tests and verification

- Use `pytest` with Home Assistant's fixtures and public interfaces. Assert config entries, the state machine, registries, services, and flow results rather than private implementation details.
- Cover pure domain rules with focused unit tests and Home Assistant wiring with integration tests. Include setup, unload/reload, migration, restoration, subscriptions, and entity-registry behavior as applicable.
- Exercise realistic failure boundaries: `unknown`/`unavailable`, non-numeric state, unsupported or changed unit, missing source, out-of-order updates, restart, reset/decrease, duplicate configuration, and partial setup.
- Verify that unload removes listeners and that repeated setup/reload does not duplicate entities, callbacks, or accumulated values.
- Avoid real sleeps and network access. Use Home Assistant time helpers, mocks at the lookup site, and deterministic state transitions.
- Run the smallest relevant tests while iterating, then the repository's complete required test, lint, formatting, and type-check commands. Report commands and exact outcomes; distinguish failures caused by the change from pre-existing or environment failures.

## Documentation and review

- Keep the manifest, config-flow strings, translations, entity names, README/integration documentation, and behavior synchronized.
- Document prerequisites, UI setup/options, accepted source semantics and units, created entities, state/statistics behavior, restart handling, limitations, troubleshooting, removal, and any privacy or data-access implications that actually apply.
- Describe implemented behavior for end users; do not promise planned features or copy Core-only submission metadata into a custom integration without a repository requirement.
- In review mode, lead with actionable findings ordered by severity and cite exact files/lines plus the violated behavior or official rule. Separate correctness defects from optional quality improvements and identify missing tests. Do not modify code when the request is review-only.
- At completion, map the result back to the issue acceptance criteria and call out any remaining decision or follow-up. Do not broaden the issue silently to make the implementation look complete.
