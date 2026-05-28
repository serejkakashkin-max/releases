import re


ARTIFACT_URL_PATTERN = re.compile(r"(?:https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}/[^\s\"'<()>;]+")
BASE_DISTRIBUTION_VERSION_PATTERN = re.compile(r"[DP]-\d+(?:\.\d+){2}-\d+")
FULL_RELEASE_VERSION_PATTERN = re.compile(r"[DP]-\d+(?:\.\d+){2}(?:[.-][A-Za-z0-9_]+)+")


def iter_nested_values(value):
    if isinstance(value, dict):
        yield value
        for nested_value in value.values():
            yield from iter_nested_values(nested_value)
    elif isinstance(value, list):
        for nested_item in value:
            yield from iter_nested_values(nested_item)
    elif value is not None:
        yield value


def _split_text_artifacts(value):
    text = str(value or "").strip()
    if not text:
        return []
    parts = [part.strip() for part in re.split(r"[;\n\r]+", text) if part.strip()]
    return parts or [text]


def flatten_artifact_candidates(values):
    candidates = []
    source_values = values if isinstance(values, (list, tuple)) else [values]
    for value in source_values:
        if isinstance(value, list):
            candidates.extend(flatten_artifact_candidates(value))
        elif isinstance(value, dict):
            candidates.append(value)
        elif value is not None:
            candidates.extend(_split_text_artifacts(value))
    return candidates


def _artifact_text(value):
    if isinstance(value, dict):
        return " ".join(str(item) for item in iter_nested_values(value) if not isinstance(item, dict))
    return str(value or "")


def is_image_artifact(value):
    text = _artifact_text(value).lower()
    if not text:
        return False
    if "registry.ca.sbrf.ru" in text or "@sha256" in text:
        return True
    return bool(re.search(r"(?:^|[\s;])(?:[a-z0-9.-]+(?::\d+)?/)+[a-z0-9._/-]+:[^\s;]+", text))


def extract_artifact_url(value):
    candidates = []
    for nested_value in iter_nested_values(value):
        if isinstance(nested_value, dict):
            continue
        raw_value = str(nested_value or "").strip()
        if not raw_value:
            continue
        candidates.extend(match.group(0).rstrip('",)') for match in ARTIFACT_URL_PATTERN.finditer(raw_value))
        if raw_value.startswith(("http://", "https://")):
            candidates.append(raw_value)

    if not candidates:
        return ""

    def _url_score(url):
        lowered = str(url or "").lower()
        score = 0
        if "maven-distr" in lowered:
            score += 20
        if "distrib.zip" in lowered:
            score += 20
        if "registry.ca.sbrf.ru" in lowered or "@sha256" in lowered:
            score -= 40
        return score

    return max(candidates, key=_url_score)


def normalize_artifact_url(url_value):
    url_value = str(url_value or "").strip()
    if not url_value:
        return ""
    if url_value.startswith(("http://", "https://")):
        return url_value
    return f"https://{url_value}"


def extract_distribution_version(value):
    preferred_values = []
    if isinstance(value, dict):
        for key in ("version", "buildVersion", "release_version", "releases_version"):
            raw_value = value.get(key)
            if raw_value:
                preferred_values.append(str(raw_value))
    preferred_values.extend(str(item) for item in iter_nested_values(value) if not isinstance(item, dict))

    for raw_value in preferred_values:
        match = BASE_DISTRIBUTION_VERSION_PATTERN.search(str(raw_value))
        if match:
            return match.group(0)
    for raw_value in preferred_values:
        match = FULL_RELEASE_VERSION_PATTERN.search(str(raw_value))
        if match:
            return match.group(0)
    return ""


def extract_artifact_ke_id(value):
    if isinstance(value, dict):
        for key in ("id", "smId", "PARENT_CI"):
            raw_ke = str(value.get(key) or "").strip()
            if raw_ke:
                return raw_ke

    text = _artifact_text(value)
    matches = re.findall(r"CI\d{6,}", text, flags=re.IGNORECASE)
    return matches[-1].upper() if matches else ""


def classify_artifact_entry(value):
    if not value:
        return "unknown"
    text = _artifact_text(value).lower()
    if "maven-distr" in text or "distrib.zip" in text:
        return "distribution"
    if is_image_artifact(value):
        return "image"

    version = extract_distribution_version(value)
    if version:
        return "distribution"
    return "unknown"


def artifact_score(value, index=0):
    kind = classify_artifact_entry(value)
    if kind == "image":
        return -1000 - index
    score = 0
    text = _artifact_text(value).lower()
    url = extract_artifact_url(value)
    version = extract_distribution_version(value)
    if kind == "distribution":
        score += 100
    if "maven-distr" in text:
        score += 30
    if "distrib.zip" in text:
        score += 30
    if url:
        score += 10
    if version:
        score += 10
        if version.lower() in str(url).lower():
            score += 5
    if extract_artifact_ke_id(value):
        score += 3
    return score - (index / 1000)


def select_distribution_artifact(values, allow_image_artifact=False):
    candidates = flatten_artifact_candidates(values)
    if not candidates:
        return None

    distribution_candidates = [
        (index, candidate)
        for index, candidate in enumerate(candidates)
        if classify_artifact_entry(candidate) == "distribution"
    ]
    if distribution_candidates:
        return max(distribution_candidates, key=lambda pair: artifact_score(pair[1], pair[0]))[1]

    if allow_image_artifact:
        image_candidates = [
            (index, candidate)
            for index, candidate in enumerate(candidates)
            if classify_artifact_entry(candidate) == "image"
        ]
        if image_candidates:
            return max(image_candidates, key=lambda pair: artifact_score(pair[1], pair[0]))[1]

    return None
