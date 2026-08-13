# Nem-sei V2 data rules

- Missing data is not zero; incomplete current periods are not final periods.
- Portfolio totals expose coverage and excluded/missing assets.
- Expected production comes from a versioned financial model; absent model
  means missing expected production.
- Provider external identifiers are provider-specific; unknown state is not
  operational; partial sync is not success.
- Future `PortfolioMember` records require temporal membership (`valid_from`,
  `valid_to`) and approved report snapshots freeze resolved membership.
- Before provider adapters, define canonical `MonitoringObservation` and
  `ProductionFact` contracts. Provider payloads do not leak into domains or
  reporting.
- Future reports consume stable canonical data after assets/mappings,
  monitoring/canonical facts, portfolio membership, providers, production,
  and finance are established.
