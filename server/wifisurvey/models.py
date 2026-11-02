"""Request/response models for API v1.

The reading payload is the public contract: one POST is one sample at one
position. The household batch shape (``/api/v1/scans``) is accepted too so the
Linux agent can keep its outbox and batching behaviour.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BSSID_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
NODE_TYPES = ("laptop", "phone", "esp32")
BANDS = ("2.4", "5", "6")


def normalise_bssid(value: str) -> str:
    value = str(value).strip().lower()
    if not BSSID_RE.match(value):
        raise ValueError("bssid must be six colon-separated hex pairs")
    return value


def normalise_node_id(value: str) -> str:
    value = str(value).strip().lower()
    if not NODE_ID_RE.match(value):
        raise ValueError(
            "node_id must be 1-64 chars of lowercase letters, digits, '.', '_' or '-'"
        )
    return value


class Position(BaseModel):
    model_config = ConfigDict(extra="ignore")

    x: float
    y: float
    z: float = 1.0
    floor: int


class Sample(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bssid: str
    ssid: str | None = None
    band: Literal["2.4", "5", "6"]
    channel: int = Field(ge=1, le=233)
    freq_mhz: int = Field(ge=2400, le=7125)
    rssi: int | None = None
    rssi_dbm: float | None = None

    @field_validator("bssid")
    @classmethod
    def _bssid(cls, value: str) -> str:
        return normalise_bssid(value)

    @field_validator("ssid")
    @classmethod
    def _ssid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = str(value)
        return value if value else None

    @model_validator(mode="after")
    def _rssi_present(self) -> Sample:
        if self.rssi is None and self.rssi_dbm is None:
            raise ValueError("rssi (dBm) is required")
        return self

    @property
    def dbm(self) -> float:
        raw = float(self.rssi if self.rssi is not None else self.rssi_dbm or 0.0)
        # Documented clamp: above -10 or below -100 is not a usable measurement.
        return max(-100.0, min(-10.0, raw))


class ReadingSubmission(BaseModel):
    """POST /api/v1/readings — documented node payload."""

    model_config = ConfigDict(extra="ignore")

    node_id: str
    ts: datetime
    position: Position
    samples: list[Sample] = Field(min_length=1, max_length=500)

    @field_validator("node_id")
    @classmethod
    def _node_id(cls, value: str) -> str:
        return normalise_node_id(value)

    @field_validator("ts")
    @classmethod
    def _ts(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("ts must include a timezone (UTC 'Z' preferred)")
        return value.astimezone(UTC)


class BatchReading(BaseModel):
    """Legacy/agent reading entry (rssi_dbm or signal_pct)."""

    model_config = ConfigDict(extra="ignore")

    bssid: str
    ssid: str | None = None
    band: Literal["2.4", "5", "6"]
    channel: int = Field(ge=1, le=233)
    freq_mhz: int = Field(ge=2400, le=7125)
    rssi_dbm: float | None = None
    signal_pct: int | None = Field(default=None, ge=0, le=100)

    @field_validator("bssid")
    @classmethod
    def _bssid(cls, value: str) -> str:
        return normalise_bssid(value)


class ScanBatchItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node_id: str
    node_type: Literal["laptop", "phone", "esp32"] = "laptop"
    scan_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    mode: Literal["background", "survey"] = "background"
    position: Position | None = None
    rssi_source: str | None = None
    readings: list[BatchReading] = Field(default_factory=list, max_length=500)

    @field_validator("node_id")
    @classmethod
    def _node_id(cls, value: str) -> str:
        return normalise_node_id(value)

    @field_validator("timestamp")
    @classmethod
    def _ts(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)


class ScanBatch(BaseModel):
    scans: list[ScanBatchItem] = Field(default_factory=list, max_length=500)


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node_id: str
    node_type: Literal["laptop", "phone", "esp32"] = "laptop"
    version: str | None = None
    iface: str | None = None
    ip: str | None = None
    last_scan_at: datetime | None = None
    buffered: int = 0
    scan_interval_s: float | None = None
    paused: bool | None = None

    @field_validator("node_id")
    @classmethod
    def _node_id(cls, value: str) -> str:
        return normalise_node_id(value)


class PairRequest(BaseModel):
    code: str = Field(min_length=6, max_length=16)
    node_id: str
    node_type: Literal["laptop", "phone", "esp32"] = "laptop"

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        value = "".join(ch for ch in str(value) if ch.isdigit())
        if len(value) != 6:
            raise ValueError("code must be six digits")
        return value

    @field_validator("node_id")
    @classmethod
    def _node_id(cls, value: str) -> str:
        return normalise_node_id(value)


class APPlacement(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ap_id: str | None = Field(default=None, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    bssids: list[str] = Field(default_factory=list, max_length=64)
    floor: int | None = None
    x: float | None = None
    y: float | None = None
    z: float | None = None
    mount: Literal["ceiling", "wall", "shelf"] | None = None
    model: str | None = None
    channels: list[int] = Field(default_factory=list)
    tx_power_dbm: float | None = None

    @field_validator("bssids")
    @classmethod
    def _bssids(cls, value: list[str]) -> list[str]:
        return [normalise_bssid(item) for item in value]

    @field_validator("ap_id")
    @classmethod
    def _ap_id(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return normalise_node_id(value)


class FloorName(BaseModel):
    index: int = Field(ge=0, le=63)
    name: str = Field(min_length=1, max_length=64)


class FloorsUpdate(BaseModel):
    floors: list[FloorName] = Field(min_length=1, max_length=64)


class AdminSetup(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class UnifiSetup(BaseModel):
    enabled: bool = True
    url: str = ""
    username: str = ""
    password: str = ""
    site: str = "default"
    verify_tls: bool = False

    @model_validator(mode="after")
    def _url_when_enabled(self) -> UnifiSetup:
        if self.enabled and not self.url:
            raise ValueError("url is required when UniFi is enabled")
        return self


class ModelNameUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
