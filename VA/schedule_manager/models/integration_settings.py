from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CalendarIntegrationSettings:
    enabled: bool = True
    provider: str = "holidays"  # теперь по умолчанию holidays
    api_url: str = ""
    api_token: str = ""
    github_config_url: str = ""
    github_branch: str = ""
    timeout_seconds: int = 5

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CalendarIntegrationSettings":
        provider = str(data.get("provider", "holidays"))
        # Разрешаем провайдеры
        allowed_providers = {"holidays", "consultant", "isdayoff", "custom", "local", "russia"}
        if provider not in allowed_providers:
            provider = "holidays"

        # Для holidays не нужен URL
        if provider in ["holidays", "local", "russia"]:
            api_url = ""
        else:
            api_url = str(data.get("api_url", ""))

        return cls(
            enabled=bool(data.get("enabled", True)),
            provider=provider,
            api_url=api_url,
            api_token=str(data.get("api_token", "")),
            github_config_url=str(data.get("github_config_url", "")),
            github_branch=str(data.get("github_branch", "")),
            timeout_seconds=int(data.get("timeout_seconds", 5) or 5),
        )