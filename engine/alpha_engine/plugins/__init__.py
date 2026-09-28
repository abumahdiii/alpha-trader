"""Uploaded strategy plugins: a user's Python file defining ONE ``Strategy``, run only in an isolated worker.

Layers (``.claude/wiki/knowledge/strategy-plugin-upload.md``, user decisions section 6-الف):

* :mod:`.validator` -- static checks in the engine process (size, UTF-8, AST: import whitelist, no
  exec/eval/open/getattr/dunder access, no banned order-API names, exactly one ``Strategy`` subclass with
  literal ``name``/``version``/``title_fa``). Also the shared banned-name scanner of ``tests/test_no_order_calls``;
* :mod:`.worker` -- the sandboxed child process (``python -m alpha_engine --plugin-worker``): MetaTrader5 and
  every non-whitelisted import blocked, audit hook against sockets/processes/file writes/reads outside the
  Python installation, restricted builtins; JSON lines over stdin/stdout, never pickle;
* :mod:`.host` -- the parent side: spawns the worker (minimal environment, Windows Job Object with memory and
  process caps, timeouts by kill), re-validates everything it returns, :class:`~.host.PluginStrategyProxy`;
* :mod:`.checks` -- dynamic validation on :mod:`.fixture` data (determinism, prefix equivalence, future
  mutation, time budget) and the full upload pipeline;
* :mod:`.store` -- files under ``data/strategies/plugins/`` + the ``strategy_plugins`` table (strict versions);
* :mod:`.template` -- the downloadable template (a string constant; PyInstaller ships no ``.py`` sources).

This package ``__init__`` imports nothing on purpose: the worker imports :mod:`.worker` before any engine or
third-party module is loaded.
"""
