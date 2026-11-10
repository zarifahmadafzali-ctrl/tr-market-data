"""GitHub Actions download engine for TR Market Data (Phase 1).

Durable state lives in the private index repo (manifest + queue/*.json) and the Telegram archive
channel; the SQLite catalog inside a runner is only an ephemeral cache rebuilt from the manifest.
"""
