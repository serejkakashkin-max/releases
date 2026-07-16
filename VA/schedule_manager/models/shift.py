from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class ShiftDefinition:
    code: str
    name: str
    short_name: str
    description: str
    color: str
    hours: float
    timezone: str
    start_time: str = ""
    end_time: str = ""
    aliases: Tuple[str, ...] = ()

    @property
    def text_color(self) -> str:
        color = self.color.lstrip("#")
        if len(color) != 6:
            return "#1f2933"
        try:
            red = int(color[0:2], 16)
            green = int(color[2:4], 16)
            blue = int(color[4:6], 16)
        except ValueError:
            return "#1f2933"
        brightness = (red * 299 + green * 587 + blue * 114) / 1000
        return "#1f2933" if brightness > 150 else "#ffffff"

    @property
    def time_range(self) -> str:
        if self.start_time and self.end_time:
            return f"{self.start_time}-{self.end_time}"
        return ""

    @property
    def display_code(self) -> str:
        return self.short_name or self.code

    @property
    def hours_display(self) -> str:
        if float(self.hours).is_integer():
            return str(int(self.hours))
        return str(self.hours)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "short_name": self.short_name,
            "description": self.description,
            "color": self.color,
            "hours": self.hours,
            "timezone": self.timezone,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "aliases": list(self.aliases),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ShiftDefinition":
        return cls(
            code=str(data.get("code", "")).strip(),
            name=str(data.get("name", "")).strip(),
            short_name=str(data.get("short_name", "")).strip(),
            description=str(data.get("description", "")).strip(),
            color=str(data.get("color", "")).strip(),
            hours=float(data.get("hours") or 0),
            timezone=str(data.get("timezone", "")).strip(),
            start_time=str(data.get("start_time", "")).strip(),
            end_time=str(data.get("end_time", "")).strip(),
            aliases=tuple(str(alias).strip() for alias in data.get("aliases", []) if str(alias).strip()),
        )
