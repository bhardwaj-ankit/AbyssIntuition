from pydantic import BaseModel


class SignalConfig(BaseModel):
    min_confidence: float = 0.55
    long_threshold: float = 0.2
    short_threshold: float = -0.2
    max_spread_bps: float = 8.0
    risk_reward: float = 1.3
    min_stop_bps: float = 8.0
    max_stop_bps: float = 70.0
