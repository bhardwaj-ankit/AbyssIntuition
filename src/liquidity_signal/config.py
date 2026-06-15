from __future__ import annotations

import os
from pathlib import Path
import json

from pydantic import BaseModel, Field


class SignalConfig(BaseModel):
    min_confidence: float = 0.55
    long_threshold: float = 0.2
    short_threshold: float = -0.2
    max_spread_bps: float = 8.0
    risk_reward: float = 1.3
    min_stop_bps: float = 8.0
    max_stop_bps: float = 70.0
    signal_horizon: str = "5m"
    signal_ttl_seconds: int = 90
    model_version: str = "signal-v0.2.0"
    feature_version: str = "features-v0.2.0"
    order_book_levels: int = 50
    recent_trade_limit: int = 200
    short_kline_limit: int = 60
    high_volatility_bps: float = 35.0
    elevated_liquidity_gap_bps: float = 2.5


class BybitDemoConfig(BaseModel):
    api_key: str = ""
    api_secret: str = ""
    base_url: str = "https://api-demo.bybit.com"
    category: str = "linear"
    account_type: str = "UNIFIED"
    recv_window: int = Field(default=5000, ge=1000, le=20000)
    default_symbol: str = "BTCUSDT"
    default_poll_interval_seconds: int = Field(default=5, ge=5, le=600)
    default_leverage: float = Field(default=2.0, ge=1.0, le=25.0)
    default_risk_per_trade_pct: float = Field(default=0.01, gt=0.0, le=0.05)
    default_max_margin_fraction: float = Field(default=0.35, gt=0.05, le=1.0)
    default_cooldown_seconds: int = Field(default=30, ge=0, le=7200)
    default_mode: str = "BALANCED"
    breakout_min_confidence: float = Field(default=0.70, ge=0.5, le=0.95)
    breakout_min_volume_ratio: float = Field(default=1.15, ge=0.8, le=3.0)
    breakout_max_failure_rate: float = Field(default=0.35, ge=0.0, le=1.0)
    target_liquidation_move_pct: float = Field(default=90.0, ge=10.0, le=95.0)
    opposite_move_close_r: float = Field(default=0.90, ge=0.25, le=2.0)
    live_flip_min_confidence: float = Field(default=0.62, ge=0.4, le=0.95)
    live_flip_min_agreement_ratio: float = Field(default=0.55, ge=0.2, le=1.0)
    config_path: str = ""
    config_source: str = "defaults"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @staticmethod
    def default_config_path() -> Path:
        return Path(__file__).resolve().parents[2] / "runtime" / "bybit_demo_config.json"

    @classmethod
    def from_sources(cls, config_path: str | None = None) -> "BybitDemoConfig":
        path = Path(config_path).expanduser() if config_path else cls.default_config_path()
        file_payload: dict[str, object] = {}
        source = "defaults"

        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    file_payload = raw
                    source = "file"
            except (OSError, ValueError, TypeError):
                file_payload = {}
                source = "defaults"

        env_values = {
            "api_key": os.getenv("BYBIT_DEMO_API_KEY", "").strip(),
            "api_secret": os.getenv("BYBIT_DEMO_API_SECRET", "").strip(),
            "base_url": os.getenv("BYBIT_DEMO_BASE_URL", "").strip(),
            "category": os.getenv("BYBIT_DEMO_CATEGORY", "").strip(),
            "account_type": os.getenv("BYBIT_DEMO_ACCOUNT_TYPE", "").strip(),
            "recv_window": os.getenv("BYBIT_DEMO_RECV_WINDOW", "").strip(),
            "default_symbol": os.getenv("BYBIT_DEMO_DEFAULT_SYMBOL", "").strip(),
            "default_poll_interval_seconds": os.getenv("BYBIT_DEMO_POLL_SECONDS", "").strip(),
            "default_leverage": os.getenv("BYBIT_DEMO_DEFAULT_LEVERAGE", "").strip(),
            "default_risk_per_trade_pct": os.getenv("BYBIT_DEMO_RISK_PER_TRADE_PCT", "").strip(),
            "default_max_margin_fraction": os.getenv("BYBIT_DEMO_MAX_MARGIN_FRACTION", "").strip(),
            "default_cooldown_seconds": os.getenv("BYBIT_DEMO_COOLDOWN_SECONDS", "").strip(),
            "default_mode": os.getenv("BYBIT_DEMO_MODE", "").strip(),
            "breakout_min_confidence": os.getenv("BYBIT_DEMO_BREAKOUT_MIN_CONFIDENCE", "").strip(),
            "breakout_min_volume_ratio": os.getenv("BYBIT_DEMO_BREAKOUT_MIN_VOLUME_RATIO", "").strip(),
            "breakout_max_failure_rate": os.getenv("BYBIT_DEMO_BREAKOUT_MAX_FAILURE_RATE", "").strip(),
            "target_liquidation_move_pct": os.getenv("BYBIT_DEMO_TARGET_LIQUIDATION_MOVE_PCT", "").strip(),
            "opposite_move_close_r": os.getenv("BYBIT_DEMO_OPPOSITE_MOVE_CLOSE_R", "").strip(),
            "live_flip_min_confidence": os.getenv("BYBIT_DEMO_LIVE_FLIP_MIN_CONFIDENCE", "").strip(),
            "live_flip_min_agreement_ratio": os.getenv("BYBIT_DEMO_LIVE_FLIP_MIN_AGREEMENT_RATIO", "").strip(),
        }
        if any(value for value in env_values.values()):
            source = "env" if source == "defaults" else "file+env"

        merged: dict[str, object] = {}
        merged.update({key: value for key, value in file_payload.items() if value not in (None, "")})
        merged.update({key: value for key, value in env_values.items() if value not in (None, "")})
        merged["config_path"] = str(path)
        merged["config_source"] = source
        return cls.model_validate(merged)

    @classmethod
    def from_env(cls) -> "BybitDemoConfig":
        return cls.from_sources()


class AISupervisorConfig(BaseModel):
    enabled: bool = False
    provider: str = "OPENAI"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout_seconds: float = Field(default=8.0, ge=2.0, le=60.0)
    max_output_tokens: int = Field(default=300, ge=100, le=1200)
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    config_path: str = ""
    config_source: str = "defaults"

    @property
    def configured(self) -> bool:
        key = self.api_key.strip()
        lowered = key.lower()
        return bool(key) and "paste_your_" not in lowered and not lowered.startswith("your_")

    @property
    def active(self) -> bool:
        return self.enabled and self.configured

    @staticmethod
    def default_config_path() -> Path:
        return Path(__file__).resolve().parents[2] / "runtime" / "ai_supervisor_config.json"

    @classmethod
    def from_sources(cls, config_path: str | None = None) -> "AISupervisorConfig":
        path = Path(config_path).expanduser() if config_path else cls.default_config_path()
        file_payload: dict[str, object] = {}
        source = "defaults"

        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    file_payload = raw
                    source = "file"
            except (OSError, ValueError, TypeError):
                file_payload = {}
                source = "defaults"

        env_values = {
            "enabled": os.getenv("AI_SUPERVISOR_ENABLED", "").strip(),
            "provider": os.getenv("AI_SUPERVISOR_PROVIDER", "").strip(),
            "api_key": os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("AI_SUPERVISOR_API_KEY", "").strip(),
            "base_url": os.getenv("OPENAI_BASE_URL", "").strip() or os.getenv("AI_SUPERVISOR_BASE_URL", "").strip(),
            "model": os.getenv("AI_SUPERVISOR_MODEL", "").strip(),
            "timeout_seconds": os.getenv("AI_SUPERVISOR_TIMEOUT_SECONDS", "").strip(),
            "max_output_tokens": os.getenv("AI_SUPERVISOR_MAX_OUTPUT_TOKENS", "").strip(),
            "temperature": os.getenv("AI_SUPERVISOR_TEMPERATURE", "").strip(),
        }
        if any(value for value in env_values.values()):
            source = "env" if source == "defaults" else "file+env"

        merged: dict[str, object] = {}
        merged.update({key: value for key, value in file_payload.items() if value not in (None, "")})
        merged.update({key: value for key, value in env_values.items() if value not in (None, "")})
        merged["config_path"] = str(path)
        merged["config_source"] = source
        return cls.model_validate(merged)

    @classmethod
    def from_env(cls) -> "AISupervisorConfig":
        return cls.from_sources()
