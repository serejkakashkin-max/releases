from dataclasses import asdict, dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Employee:
    name: str
    email: str = ""
    phone: str = ""
    status: str = "active"
    personnel_number: Optional[str] = None
    role: Optional[str] = None
    location: Optional[str] = None
    competencies: Tuple[str, ...] = ()
    overtime_ready: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Employee":
        competencies = tuple(
            str(competency).strip()
            for competency in data.get("competencies", [])
            if str(competency).strip()
        )
        if not competencies:
            legacy_role = data.get("role")
            if legacy_role == "manager":
                competencies = ("manager",)
            elif legacy_role == "employee":
                competencies = ("support",)
        return cls(
            name=data["name"],
            email=data.get("email", ""),
            phone=data.get("phone", ""),
            status=data.get("status", "active"),
            personnel_number=data.get("personnel_number"),
            role=data.get("role"),
            location=data.get("location"),
            competencies=competencies,
            overtime_ready=bool(data.get("overtime_ready", True)),
        )
