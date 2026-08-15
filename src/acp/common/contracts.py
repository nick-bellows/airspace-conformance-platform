"""Wire contracts for every Kafka topic.

These models are the only agreement between services; services never import each
other. Each model is frozen and forbids unknown fields, so a producer that adds a
field without regenerating `contracts/*.json` fails the CI drift gate rather than
silently breaking a consumer.

Versioning rule: additive, optional fields are a compatible change and keep the
same topic suffix. Removing a field, renaming one, or narrowing a type is a
breaking change and requires a new topic (``.v2``) plus a dual-write window.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Bump when the meaning of an existing field changes. A bump invalidates cached
# datasets and requires regenerating every committed evaluation report.
CONTRACTS_VERSION = "acp-contracts-v1"

TOPIC_SURVEILLANCE_REPORTS = "surveillance.reports.v1"
TOPIC_TRACK_UPDATES = "tracks.updates.v1"
TOPIC_ALERTS = "airspace.alerts.v1"
TOPIC_SIM_TRUTH = "sim.truth.v1"

Latitude = Annotated[float, Field(ge=-90.0, le=90.0)]
Longitude = Annotated[float, Field(ge=-180.0, le=180.0)]
Bearing = Annotated[float, Field(ge=0.0, lt=360.0)]
Icao24 = Annotated[str, Field(pattern=r"^[0-9a-f]{6}$")]
Squawk = Annotated[str, Field(pattern=r"^[0-7]{4}$")]


class DataSource(StrEnum):
    """Provenance of a message.

    Every message declares where it came from. There is currently one value; it
    exists so that a synthetic record can never be mistaken for a real one if a
    live feed is ever added.
    """

    SIMULATOR = "simulator"


class Frozen(BaseModel):
    """Base for all wire models: immutable, and unknown fields are an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SurveillanceReport(Frozen):
    """One position report for one aircraft, as observed by the sensor layer.

    This is the *noisy* view. Fields are optional wherever a real ADS-B message
    may omit them, so downstream code cannot assume completeness.
    """

    schema_version: Literal["1"] = "1"
    report_id: str
    icao24: Icao24
    callsign: str | None = None
    observed_at: datetime
    lat: Latitude
    lon: Longitude
    altitude_baro_ft: float | None = None
    ground_speed_kt: Annotated[float, Field(ge=0.0)] | None = None
    track_deg: Bearing | None = None
    vertical_rate_fpm: float | None = None
    squawk: Squawk | None = None
    on_ground: bool = False
    source: DataSource = DataSource.SIMULATOR
    scenario_id: str | None = None


class TrackState(StrEnum):
    """Lifecycle of a track inside the tracker."""

    INITIATING = "initiating"
    CONFIRMED = "confirmed"
    COASTING = "coasting"
    TERMINATED = "terminated"


class TrackUpdate(Frozen):
    """A filtered state estimate for one aircraft.

    Unlike :class:`SurveillanceReport` the kinematic fields are always present:
    the filter always has an estimate, even while coasting through a dropout.
    ``position_uncertainty_m`` is how much to trust it.
    """

    schema_version: Literal["1"] = "1"
    track_id: str
    icao24: Icao24
    callsign: str | None = None
    updated_at: datetime
    last_report_at: datetime
    state: TrackState
    lat: Latitude
    lon: Longitude
    altitude_ft: float
    ground_speed_kt: Annotated[float, Field(ge=0.0)]
    track_deg: Bearing
    vertical_rate_fpm: float
    turn_rate_deg_s: float
    position_uncertainty_m: Annotated[float, Field(ge=0.0)]
    update_count: Annotated[int, Field(ge=0)]
    #: How far this aircraft was from where constant-velocity physics predicted,
    #: in nautical miles, at the last correction. This is the filter's surprise:
    #: small when the aircraft flies as modelled, large when it manoeuvres.
    #: Optional because a track that has only ever coasted has no innovation.
    innovation_nm: Annotated[float, Field(ge=0.0)] | None = None
    squawk: Squawk | None = None
    source: DataSource = DataSource.SIMULATOR
    scenario_id: str | None = None


class AlertKind(StrEnum):
    """What kind of condition was detected."""

    PREDICTED_CONFLICT = "predicted_conflict"
    NON_CONFORMANCE = "non_conformance"
    EMERGENCY_SQUAWK = "emergency_squawk"
    EXCESSIVE_DESCENT = "excessive_descent"


class Severity(StrEnum):
    """Advisory severity. This system never issues a control instruction."""

    INFO = "info"
    ADVISORY = "advisory"
    CAUTION = "caution"
    WARNING = "warning"


class AlertState(StrEnum):
    """Alert lifecycle, used to suppress flapping.

    An alert is raised once as ``NEW``, re-published as ``SUSTAINED`` while the
    condition persists, and closed as ``CLEARED``. Consumers key on
    ``alert_key`` and keep only the latest state.
    """

    NEW = "new"
    SUSTAINED = "sustained"
    CLEARED = "cleared"


class ConflictEvidence(Frozen):
    """Geometry behind a predicted loss of separation.

    Distances are the *predicted minimum* over the lookahead window, not the
    current separation.
    """

    time_to_cpa_s: float
    min_horizontal_sep_nm: Annotated[float, Field(ge=0.0)]
    min_vertical_sep_ft: Annotated[float, Field(ge=0.0)]
    lookahead_s: Annotated[float, Field(gt=0.0)]
    horizontal_standard_nm: Annotated[float, Field(gt=0.0)]
    vertical_standard_ft: Annotated[float, Field(gt=0.0)]


class ConformanceEvidence(Frozen):
    """How far an aircraft diverged from where it was predicted to be."""

    predicted_at: datetime
    horizon_s: Annotated[float, Field(gt=0.0)]
    error_nm: Annotated[float, Field(ge=0.0)]
    baseline_error_nm: Annotated[float, Field(ge=0.0)]
    threshold_nm: Annotated[float, Field(gt=0.0)]
    predictor_version: str


class Alert(Frozen):
    """An advisory raised by the conformance service.

    ``reason_codes`` carries machine-readable justifications so an alert is
    always explainable without re-running the detector.
    """

    schema_version: Literal["1"] = "1"
    alert_id: str
    alert_key: str
    kind: AlertKind
    severity: Severity
    state: AlertState
    raised_at: datetime
    updated_at: datetime
    track_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    summary: str
    conflict: ConflictEvidence | None = None
    conformance: ConformanceEvidence | None = None
    source: DataSource = DataSource.SIMULATOR
    scenario_id: str | None = None


class TruthState(Frozen):
    """Noiseless simulator state, published for evaluation only.

    No production code path consumes this topic. It exists so the conflict
    detector can be scored against ground truth it never sees, which is what
    makes those metrics meaningful despite the data being synthetic.
    """

    schema_version: Literal["1"] = "1"
    icao24: Icao24
    scenario_id: str
    sim_version: str
    valid_at: datetime
    lat: Latitude
    lon: Longitude
    altitude_ft: float
    ground_speed_kt: Annotated[float, Field(ge=0.0)]
    track_deg: Bearing
    vertical_rate_fpm: float
    phase: str


#: Every wire model, keyed by the topic it travels on. Drives schema generation
#: and the CI drift gate; adding a topic without adding it here fails the gate.
TOPIC_MODELS: dict[str, type[Frozen]] = {
    TOPIC_SURVEILLANCE_REPORTS: SurveillanceReport,
    TOPIC_TRACK_UPDATES: TrackUpdate,
    TOPIC_ALERTS: Alert,
    TOPIC_SIM_TRUTH: TruthState,
}
