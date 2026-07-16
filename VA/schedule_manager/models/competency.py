from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Competency:
    code: str
    name: str
    description: str = ""
    is_system: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Competency":
        return cls(
            code=str(data.get("code", "")).strip(),
            name=str(data.get("name", "")).strip(),
            description=str(data.get("description", "")).strip(),
            is_system=bool(data.get("is_system", False)),
        )
