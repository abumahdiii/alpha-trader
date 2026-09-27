"""Market-data layer: canonical OHLCV schema, broker-time conversion, Parquet cache and gap analysis.

Nothing in this package imports ``MetaTrader5``; the only module that talks to the terminal is
:mod:`alpha_engine.mt5_adapter`.
"""
