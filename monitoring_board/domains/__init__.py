"""Feature modules for the modular-monolith migration.

Domains may depend on shared utilities, repositories and services, but never on
the Flask composition root in :mod:`monitoring_board.app_factory`.
"""
