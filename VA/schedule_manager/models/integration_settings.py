from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CalendarIntegrationSettings:
    enabled: bool = True
    provider: str = "consultant"
    api_url: str = "https://www.consultant.ru/law/ref/calendar/proizvodstvennye/"
    api_token: str = ""
    github_config_url: str = ""
    github_branch: str = ""
    timeout_seconds: int = 5

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CalendarIntegrationSettings":
        provider = str(data.get("provider", "consultant"))
        if provider not in {"consultant", "isdayoff", "custom"}:
            provider = "consultant"
        default_url = {
            "consultant": "https://www.consultant.ru/law/ref/calendar/proizvodstvennye/",
            "isdayoff": "https://isdayoff.ru/api/getdata",
            "custom": "",
        }[provider]
        api_url = str(data.get("api_url", default_url))
        if provider == "consultant" and not api_url:
            api_url = default_url
        if provider == "isdayoff" and not api_url:
            api_url = default_url
        return cls(
            enabled=bool(data.get("enabled", True)),
            provider=provider,
            api_url=api_url,
            api_token=str(data.get("api_token", "")),
            github_config_url=str(data.get("github_config_url", "")),
            github_branch=str(data.get("github_branch", "")),
            timeout_seconds=int(data.get("timeout_seconds", 5) or 5),
        )
