from __future__ import annotations

import json
import time
from typing import Any

import httpx

from liquidity_signal.config import AISupervisorConfig
from liquidity_signal.models import AITradeDecision, AISupervisorConfigStatus


class OpenAISupervisor:
    def __init__(
        self,
        *,
        cfg: AISupervisorConfig | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.cfg = cfg or AISupervisorConfig.from_env()
        self._client = client or httpx.Client(
            base_url=self.cfg.base_url.rstrip("/"),
            timeout=self.cfg.timeout_seconds,
        )
        self._client_injected = client is not None

    def close(self) -> None:
        if not self._client_injected:
            self._client.close()

    def _reload_config(self) -> None:
        if self._client_injected:
            return
        next_cfg = AISupervisorConfig.from_sources(self.cfg.config_path or None)
        changed = (
            next_cfg.base_url != self.cfg.base_url
            or next_cfg.timeout_seconds != self.cfg.timeout_seconds
        )
        self.cfg = next_cfg
        if changed:
            self._client.close()
            self._client = httpx.Client(
                base_url=self.cfg.base_url.rstrip("/"),
                timeout=self.cfg.timeout_seconds,
            )

    def is_active(self) -> bool:
        self._reload_config()
        return self.cfg.active

    def get_config_status(self) -> AISupervisorConfigStatus:
        self._reload_config()
        if self.cfg.active:
            message = "AI supervisor is active and will validate entries plus monitor open positions."
        elif self.cfg.enabled and not self.cfg.configured:
            message = f"AI supervisor is enabled but missing an OpenAI API key in {self.cfg.config_path}."
        elif self.cfg.configured:
            message = "AI supervisor key is configured but disabled. Set enabled=true to turn it on."
        else:
            message = f"Paste your OpenAI API key into {self.cfg.config_path} to enable AI supervision."
        return AISupervisorConfigStatus(
            enabled=self.cfg.enabled,
            configured=self.cfg.configured,
            active=self.cfg.active,
            provider=self.cfg.provider,
            model=self.cfg.model,
            config_path=self.cfg.config_path,
            config_source=self.cfg.config_source,
            message=message,
        )

    def review_entry(self, snapshot: dict[str, Any]) -> AITradeDecision | None:
        return self._review("entry", snapshot)

    def review_position(self, snapshot: dict[str, Any]) -> AITradeDecision | None:
        return self._review("position", snapshot)

    def _review(self, review_type: str, snapshot: dict[str, Any]) -> AITradeDecision | None:
        self._reload_config()
        if not self.cfg.active:
            return None

        try:
            response = self._client.post(
                "/responses",
                headers={
                    "Authorization": f"Bearer {self.cfg.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.cfg.model,
                    "temperature": self.cfg.temperature,
                    "max_output_tokens": self.cfg.max_output_tokens,
                    "input": [
                        {
                            "role": "system",
                            "content": [{"type": "input_text", "text": self._system_prompt(review_type)}],
                        },
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": self._user_prompt(review_type, snapshot)}],
                        },
                    ],
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "ai_trade_decision",
                            "strict": True,
                            "schema": self._schema(),
                        }
                    },
                },
            )
            response.raise_for_status()
            payload = response.json()
            content = self._extract_text(payload)
            if not content:
                raise RuntimeError("AI supervisor returned an empty response")
            parsed = json.loads(content)
            parsed["enabled"] = True
            parsed["source"] = self.cfg.provider
            parsed["model"] = self.cfg.model
            parsed["review_type"] = review_type
            parsed["reviewed_at"] = int(time.time() * 1000)
            return AITradeDecision.model_validate(self._clamp_decision(parsed))
        except Exception as exc:
            return AITradeDecision(
                enabled=True,
                source=self.cfg.provider,
                model=self.cfg.model,
                review_type=review_type,
                entry_verdict="SKIPPED",
                exit_action="HOLD",
                reason=f"AI supervisor unavailable: {exc}",
                risk_flags=["supervisor_unavailable"],
                reviewed_at=int(time.time() * 1000),
            )

    @staticmethod
    def _schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "entry_verdict": {
                    "type": "string",
                    "enum": ["ALLOW", "BLOCK", "ALLOW_REDUCED", "SKIPPED"],
                },
                "size_multiplier": {"type": "number"},
                "confidence_adjustment": {"type": "number"},
                "exit_action": {
                    "type": "string",
                    "enum": ["HOLD", "EXIT_NOW", "MOVE_STOP", "TAKE_PARTIAL", "TRAIL_STOP"],
                },
                "stop_adjustment_pct": {"type": "number"},
                "take_profit_adjustment_pct": {"type": "number"},
                "reason": {"type": "string"},
                "risk_flags": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "entry_verdict",
                "size_multiplier",
                "confidence_adjustment",
                "exit_action",
                "stop_adjustment_pct",
                "take_profit_adjustment_pct",
                "reason",
                "risk_flags",
            ],
        }

    @staticmethod
    def _clamp_decision(parsed: dict[str, Any]) -> dict[str, Any]:
        parsed["size_multiplier"] = max(0.25, min(float(parsed.get("size_multiplier", 1.0) or 1.0), 1.0))
        parsed["confidence_adjustment"] = max(
            -0.12,
            min(float(parsed.get("confidence_adjustment", 0.0) or 0.0), 0.12),
        )
        parsed["stop_adjustment_pct"] = max(
            -0.05,
            min(float(parsed.get("stop_adjustment_pct", 0.0) or 0.0), 0.15),
        )
        parsed["take_profit_adjustment_pct"] = max(
            -0.10,
            min(float(parsed.get("take_profit_adjustment_pct", 0.0) or 0.0), 0.15),
        )
        parsed["reason"] = str(parsed.get("reason", "")).strip()[:280]
        parsed["risk_flags"] = [str(flag).strip()[:60] for flag in parsed.get("risk_flags", [])[:6] if str(flag).strip()]
        return parsed

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        outputs = payload.get("output", [])
        if not isinstance(outputs, list):
            return ""

        fragments: list[str] = []
        for item in outputs:
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    fragments.append(text.strip())
        return "\n".join(fragments).strip()

    @staticmethod
    def _system_prompt(review_type: str) -> str:
        return (
            "You are a risk-bounded crypto trade supervisor. Review the structured trading snapshot and return only a "
            "JSON object matching the schema. Never invent prices or indicators. Respect the deterministic strategy as "
            f"the primary system. This review is for {review_type}. Use BLOCK only for materially weak or risky setups. "
            "Use ALLOW_REDUCED when the setup is valid but deserves smaller size. For open positions, EXIT_NOW only for "
            "clear invalidation, sharp adverse conditions, or crowding risk. Keep the reason concise."
        )

    @staticmethod
    def _user_prompt(review_type: str, snapshot: dict[str, Any]) -> str:
        return (
            f"Review type: {review_type}\n"
            "Return a single JSON object following the schema.\n"
            f"Snapshot:\n{json.dumps(snapshot, separators=(',', ':'), default=str)}"
        )
