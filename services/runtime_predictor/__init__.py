"""Phase 6 M5 — runtime predictor service.

Trains a LightGBM regressor on log-runtime, exposes /predict over HTTP for
the M2 Lua plugin (M6 wiring). Cold-start fallback uses
``min(user_time_limit, 4*3600)`` until the model has seen >= 100 samples.
"""
