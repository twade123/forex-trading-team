"""Shared Kronos-base inference service.

Single model instance loaded at process startup. Used by both KronosHunter
(batched 13-pair forecasts) and KronosFilter (single-pair veto checks).

Graceful degradation: if the model fails to load, the service stays alive
in a degraded state where forecast() returns None and forecast_batch() returns
an empty dict. Callers fail-open (trading continues normally).

Clears MPS memory after every call to prevent slow bloat over hundreds of
forecasts (see collective/kronos/00-kronos-overview.md lesson 5).
"""

from __future__ import annotations

import gc
import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

import pandas as pd

logger = logging.getLogger("trading_bot.kronos_inference")


@dataclass
class ForecastResult:
    pair: str
    direction: str               # "buy" | "sell" — NOW from early bars (0-3)
    drift_pips: float            # NOW early_drift (bars 0-3 mean - current), signed
    drift_atr_frac: float        # drift / ATR in pips
    confidence: float            # |drift| / cone (kept for backwards compat)
    forecast_terminal: float
    forecast_max_high: float
    forecast_min_low: float
    latency_ms: int
    # Path-interpretation fields (2026-04-16 overhaul)
    early_drift_pips: float = 0.0       # mean(bars 0-3 close) - current, pips
    terminal_drift_pips: float = 0.0    # bar[-1] close - current, pips
    early_direction: str = "buy"        # from early bars
    terminal_direction: str = "buy"     # from terminal bar
    consensus: bool = True              # early == terminal direction
    forecast_sl_price: float = 0.0      # model's worst-case price for SL
    forecast_tp_price: float = 0.0      # model's best-case price for TP
    forecast_path: list = None  # full 24-bar [{o,h,l,c}, ...] for path analysis


def _pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair.upper() else 0.0001


def _default_model_loader() -> Tuple[object, object, object]:
    """Load Kronos tokenizer + model + predictor. Returns (tokenizer, model, predictor).

    Reads model_name and tokenizer_path from TUNING — supports both HuggingFace
    hub paths (NeoQuasar/Kronos-base) and local finetuned paths.

    Import inline so unit tests don't force a real torch/MPS load."""
    import sys
    from pathlib import Path
    research_root = Path("~/Jarvis/research/kronos")
    if str(research_root) not in sys.path:
        sys.path.insert(0, str(research_root))
    from model import Kronos, KronosTokenizer, KronosPredictor  # type: ignore

    from tuning_config import TUNING
    model_name = TUNING["kronos.model_name"]["value"]
    tokenizer_path = TUNING.get("kronos.tokenizer_path", {}).get("value", "NeoQuasar/Kronos-Tokenizer-base")

    tokenizer = KronosTokenizer.from_pretrained(tokenizer_path)
    model = Kronos.from_pretrained(model_name)

    import torch
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)
    logger.info("Kronos loaded on %s (model=%s, tokenizer=%s)", device, model_name, tokenizer_path)
    return tokenizer, model, predictor


class KronosInferenceService:
    """Encapsulates a loaded Kronos model and exposes forecast helpers."""

    def __init__(self, model_loader: Callable[[], Tuple[object, object, object]] = _default_model_loader):
        self._tokenizer = None
        self._model = None
        self._predictor = None
        try:
            self._tokenizer, self._model, self._predictor = model_loader()
            self._ready = self._predictor is not None
        except Exception as exc:
            logger.error("Kronos model failed to load: %s — running in degraded mode", exc)
            self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    # ------------------------------------------------------------------
    def _build_timestamps(self, candles: pd.DataFrame, pred_len: int) -> Tuple[pd.Series, pd.Series]:
        """Kronos expects explicit datetime series for past + future bars."""
        x_ts = candles["time"]
        last = x_ts.iloc[-1]
        step = x_ts.iloc[-1] - x_ts.iloc[-2] if len(x_ts) >= 2 else pd.Timedelta(minutes=15)
        y_ts = pd.Series([last + step * (i + 1) for i in range(pred_len)])
        return x_ts, y_ts

    def _atr_pips(self, candles: pd.DataFrame, pair: str, n: int = 14) -> float:
        high = candles["high"].values
        low = candles["low"].values
        close = candles["close"].values
        if len(close) < n + 1:
            return 0.0
        tr = [max(high[i] - low[i],
                  abs(high[i] - close[i - 1]),
                  abs(low[i] - close[i - 1]))
              for i in range(1, len(close))]
        atr_raw = sum(tr[-n:]) / n
        return atr_raw / _pip_size(pair)

    def _prepare_ohlcv(self, candles: pd.DataFrame) -> pd.DataFrame:
        """Build the 6-column input for the finetuned model.

        Columns 1-4: open, high, low, close (raw candle data)
        Column 5 (volume): EMA separation (E21 - E100, signed)
        Column 6 (amount): Bollinger Band width (upper - lower)

        When using the generic Kronos-base model (not finetuned), this method
        is skipped and raw volume/amount are sent instead. Controlled by the
        ``kronos.use_indicator_columns`` tuning flag.
        """
        import numpy as np
        closes = candles["close"].values.astype(float)

        # EMA 21
        e21 = np.full(len(closes), np.nan)
        if len(closes) >= 21:
            e21[20] = closes[:21].mean()
            k21 = 2.0 / 22
            for i in range(21, len(closes)):
                e21[i] = closes[i] * k21 + e21[i - 1] * (1 - k21)

        # EMA 100
        e100 = np.full(len(closes), np.nan)
        if len(closes) >= 100:
            e100[99] = closes[:100].mean()
            k100 = 2.0 / 101
            for i in range(100, len(closes)):
                e100[i] = closes[i] * k100 + e100[i - 1] * (1 - k100)

        ema_sep = e21 - e100  # signed: positive = bullish

        # BB width (20-period, 2 std)
        bb_w = np.full(len(closes), np.nan)
        for i in range(19, len(closes)):
            w = closes[i - 19:i + 1]
            bb_w[i] = 4.0 * np.std(w)  # 2 × 2σ = upper - lower

        ohlcv = candles[["open", "high", "low", "close"]].copy()
        ohlcv["volume"] = ema_sep
        ohlcv["amount"] = bb_w
        ohlcv = ohlcv.fillna(0.0)
        return ohlcv

    def _cleanup(self) -> None:
        try:
            import torch
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass
        gc.collect()

    # ------------------------------------------------------------------
    def forecast(
        self,
        pair: str,
        candles: pd.DataFrame,
        pred_len: int = 24,
        sample_count: int = 5,
        temperature: float = 1.0,
        top_p: float = 0.9,
    ) -> Optional[ForecastResult]:
        if not self._ready:
            return None
        t0 = time.time()
        try:
            x_ts, y_ts = self._build_timestamps(candles, pred_len)
            # Use indicator columns (EMA sep + BB width) when finetuned model is active
            from tuning_config import TUNING
            use_indicators = TUNING.get("kronos.use_indicator_columns", {}).get("value", False)
            if use_indicators:
                ohlcv = self._prepare_ohlcv(candles)
            else:
                ohlcv = candles[["open", "high", "low", "close", "volume"]].copy()
                if "amount" in candles.columns:
                    ohlcv["amount"] = candles["amount"]
            fut = self._predictor.predict(
                df=ohlcv,
                x_timestamp=x_ts,
                y_timestamp=y_ts,
                pred_len=pred_len,
                T=temperature,
                top_p=top_p,
                sample_count=sample_count,
                verbose=False,
            )
            result = self._to_result(pair, candles, fut, t0)
        except Exception as exc:
            logger.warning("Kronos forecast failed for %s: %s", pair, exc)
            return None
        finally:
            self._cleanup()
        return result

    def forecast_batch(
        self,
        candles_by_pair: Dict[str, pd.DataFrame],
        pred_len: int = 24,
        sample_count: int = 5,
        temperature: float = 1.0,
        top_p: float = 0.9,
    ) -> Dict[str, ForecastResult]:
        if not self._ready or not candles_by_pair:
            return {}
        t0 = time.time()
        pairs = list(candles_by_pair.keys())
        # Kronos predict_batch requires all series to have identical historical
        # length. OANDA can return 255 or 256 depending on incomplete-bar
        # filtering. Trim every series to the shortest length so the batch is
        # well-formed.
        min_len = min(len(candles_by_pair[p]) for p in pairs)
        ohlcv_list, x_list, y_list = [], [], []
        from tuning_config import TUNING
        use_indicators = TUNING.get("kronos.use_indicator_columns", {}).get("value", False)
        for p in pairs:
            c = candles_by_pair[p].tail(min_len).reset_index(drop=True)
            candles_by_pair[p] = c  # keep trimmed copy for downstream _to_result
            if use_indicators:
                ohlcv = self._prepare_ohlcv(c)
            else:
                ohlcv = c[["open", "high", "low", "close", "volume"]].copy()
                if "amount" in c.columns:
                    ohlcv["amount"] = c["amount"]
            x_ts, y_ts = self._build_timestamps(c, pred_len)
            ohlcv_list.append(ohlcv)
            x_list.append(x_ts)
            y_list.append(y_ts)
        try:
            futs = self._predictor.predict_batch(
                df_list=ohlcv_list,
                x_timestamp_list=x_list, y_timestamp_list=y_list,
                pred_len=pred_len, T=temperature, top_p=top_p,
                sample_count=sample_count, verbose=False,
            )
            out: Dict[str, ForecastResult] = {}
            for pair, hist_key, fut in zip(pairs, pairs, futs):
                hist = candles_by_pair[hist_key]
                out[pair] = self._to_result(pair, hist, fut, t0)
            return out
        except Exception as exc:
            logger.warning("Kronos batch forecast failed: %s", exc)
            return {}
        finally:
            self._cleanup()

    def _to_result(
        self, pair: str, history: pd.DataFrame, future: pd.DataFrame, t0: float
    ) -> ForecastResult:
        pip = _pip_size(pair)
        last_close = float(history["close"].iloc[-1])

        # Terminal bar (what we used to use for everything)
        terminal = float(future["close"].iloc[-1])
        terminal_drift = terminal - last_close
        terminal_drift_pips = round(terminal_drift / pip, 2)
        terminal_direction = "buy" if terminal_drift >= 0 else "sell"

        # Early bars (0-3) — most accurate, matches 1-3 bar win profile
        early_closes = future["close"].iloc[:4]
        early_drift = float(early_closes.mean()) - last_close
        early_drift_pips = round(early_drift / pip, 2)
        early_direction = "buy" if early_drift >= 0 else "sell"

        # Consensus: early and terminal must agree on direction
        consensus = (early_direction == terminal_direction)

        # Direction is now from early bars
        direction = early_direction
        drift_pips = early_drift_pips

        # ATR and confidence (backwards compat)
        atr = self._atr_pips(history, pair)
        drift_atr_frac = drift_pips / atr if atr > 0 else 0.0
        max_high = float(future["high"].max())
        min_low = float(future["low"].min())
        cone_pips = (max_high - min_low) / pip
        confidence = abs(drift_pips) / cone_pips if cone_pips > 0 else 0.0

        # Forecast-informed SL/TP prices
        if direction == "buy":
            forecast_sl_price = min_low      # worst predicted low
            forecast_tp_price = max_high     # best predicted high
        else:
            forecast_sl_price = max_high     # worst predicted high (for short)
            forecast_tp_price = min_low      # best predicted low (for short)

        # Save full 24-bar path for path-based trade planning
        forecast_path = []
        for idx in range(len(future)):
            frow = future.iloc[idx]
            forecast_path.append({
                "o": round(float(frow["open"]), 6),
                "h": round(float(frow["high"]), 6),
                "l": round(float(frow["low"]), 6),
                "c": round(float(frow["close"]), 6),
            })

        latency_ms = int(1000 * (time.time() - t0))
        return ForecastResult(
            pair=pair, direction=direction,
            drift_pips=drift_pips,
            drift_atr_frac=round(drift_atr_frac, 3),
            confidence=round(confidence, 3),
            forecast_terminal=round(terminal, 6),
            forecast_max_high=round(max_high, 6),
            forecast_min_low=round(min_low, 6),
            latency_ms=latency_ms,
            early_drift_pips=early_drift_pips,
            terminal_drift_pips=terminal_drift_pips,
            early_direction=early_direction,
            terminal_direction=terminal_direction,
            consensus=consensus,
            forecast_sl_price=round(forecast_sl_price, 6),
            forecast_tp_price=round(forecast_tp_price, 6),
            forecast_path=forecast_path,
        )
