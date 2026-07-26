from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ManagedVaEmployee:
    employee_id: str
    name: str
    email: str
    phone: str
    personnel_number: Optional[str]
    location: str
    enabled: bool
    order: int
    status: str
    role: str
    competencies: Tuple[str, ...]
    overtime_ready: bool

    def to_dict(self) -> dict:
        return {
            "employee_id": self.employee_id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "personnel_number": self.personnel_number,
            "location": self.location,
            "enabled": self.enabled,
            "order": self.order,
            "status": self.status,
            "role": self.role,
            "competencies": list(self.competencies),
            "overtime_ready": self.overtime_ready,
        }
