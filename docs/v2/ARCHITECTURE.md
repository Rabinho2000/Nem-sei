# Nem-sei V2 architecture

V2 is a Flask modular monolith. Web, scheduler, and one worker run as separate
processes against one V2-only SQLite database. Routes call services, services
call repositories, and repositories call the database. Scheduler only enqueues
jobs; worker claims and executes them. V2 never imports V1.

Persist timestamps in UTC and render dates/times in Europe/Lisbon at the UI
boundary. V2 migrations are Alembic-only and run explicitly before application
roles start.
