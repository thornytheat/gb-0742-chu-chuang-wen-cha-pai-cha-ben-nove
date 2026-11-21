from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.timebase import seconds

IDENT = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$")]


class Window(BaseModel):
    start: str
    end: str

    @model_validator(mode="after")
    def ordered(self):
        if seconds(self.start) >= seconds(self.end):
            raise ValueError("时窗结束须晚于开始")
        return self


class Cabinet(BaseModel):
    id: IDENT
    label: str
    upper_mm: int = Field(ge=150, le=5000)
    lower_mm: int = Field(ge=0, le=4500)
    temp_limit: Decimal = Field(gt=0, le=20)
    rh_limit: Decimal = Field(gt=0, le=100)

    @model_validator(mode="after")
    def valid_separation(self):
        if self.upper_mm - self.lower_mm < 100:
            raise ValueError("上下探头须相隔至少 100 mm")
        return self


class Deployment(Window):
    probe: IDENT
    cabinet: IDENT
    level: Literal["upper", "lower"]


class Ticket(BaseModel):
    id: IDENT
    cabinet: IDENT
    at: str

    @model_validator(mode="after")
    def valid_time(self):
        seconds(self.at)
        return self


class Shift(Window):
    cabinet_hours: list[Cabinet] = Field(min_length=1, max_length=8)
    sample: IDENT
    label: str
    shift: IDENT
    csv: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,30}\.csv$")]
    records: int = Field(ge=1, le=4000)
    tolerance_seconds: int = Field(ge=0, le=900)
    late_seconds: int = Field(ge=0, le=86_400)
    sustained_hours: int = Field(ge=2, le=12)
    deployments: list[Deployment] = Field(min_length=1, max_length=64)
    tickets: list[Ticket] = Field(max_length=100)

    @model_validator(mode="after")
    def check_shift(self):
        duration = seconds(self.end) - seconds(self.start)
        if not (0 < duration <= 168 * 3600) or duration % 3600 or seconds(self.start) % 3600:
            raise ValueError("班次使用整点小时，最长七天")
        cabinets = {item.id for item in self.cabinet_hours}
        if len(cabinets) != len(self.cabinet_hours):
            raise ValueError("柜号重复")
        if any(item.cabinet not in cabinets for item in self.deployments):
            raise ValueError("部署柜未登记")
        if len({item.id for item in self.tickets}) != len(self.tickets):
            raise ValueError("工单号重复")
        return self


@dataclass(frozen=True)
class Package:
    spec: Shift
    blob: bytes


class SourceFile(BaseModel):
    role: Literal["first", "second"]
    csv: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,30}\.csv$")]
    records: int = Field(ge=1, le=4000)


class Continuation(BaseModel):
    sample: IDENT
    label: str
    shift: IDENT
    start: str
    end: str
    cabinet_hours: list[Cabinet] = Field(min_length=1, max_length=8)
    tolerance_seconds: int = Field(ge=0, le=900)
    late_seconds: int = Field(ge=0, le=86_400)
    sustained_hours: int = Field(ge=2, le=12)
    deployments: list[Deployment] = Field(min_length=1, max_length=64)
    tickets: list[Ticket] = Field(max_length=100)
    sources: list[SourceFile] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def check_continuation(self):
        duration = seconds(self.end) - seconds(self.start)
        if not (0 < duration <= 168 * 3600) or duration % 3600 or seconds(self.start) % 3600:
            raise ValueError("班次使用整点小时，最长七天")
        cabinets = {item.id for item in self.cabinet_hours}
        if len(cabinets) != len(self.cabinet_hours):
            raise ValueError("柜号重复")
        if any(item.cabinet not in cabinets for item in self.deployments):
            raise ValueError("部署柜未登记")
        if len({item.id for item in self.tickets}) != len(self.tickets):
            raise ValueError("工单号重复")
        roles = [item.role for item in self.sources]
        if sorted(roles) != ["first", "second"]:
            raise ValueError("接续原档须按先档、后档各一份登记")
        if len({item.csv for item in self.sources}) != 2:
            raise ValueError("两份原档不能为同一文件")
        return self

    def to_shift(self, csv_name, records):
        return Shift.model_validate({**self.model_dump(exclude={"sources"}), "csv": csv_name, "records": records})


@dataclass(frozen=True)
class ContinuationPackage:
    spec: Continuation
    blobs: dict[str, bytes]

    def package(self, source):
        return Package(self.spec.to_shift(source.csv, source.records), self.blobs[source.csv])
