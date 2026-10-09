"""Load and validate all service configuration.

Sources:
  .env                      (or real environment variables, which take precedence)
  table_mapping.yaml        database object names
  whatsapp_templates.yaml   template layouts

Fails fast: every problem is collected and reported together, and secret
values are never included in any message.

Check a configuration without starting the worker:
    python -m app.config
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Names that are interpolated into SQL must be plain identifiers.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_JSON_KEY = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_FOLDER = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9.-]+$")

PARAM_SOURCES = frozenset(
    {"pdf_url", "visitor_name", "slip_number", "event_type", "entry_time", "exit_time"}
)
COMPONENT_TYPES = frozenset({"header", "body", "button"})
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})

SOURCE_KEYS = (
    "table", "primary_key", "phone", "entry_time", "exit_time",
    "slip_number", "is_appointment", "is_regular", "details_json",
)
DETAILS_JSON_KEYS = (
    "visitor_name", "whom_to_visit", "company_name", "department", "visit_purpose",
    "block", "floor", "room", "visitor_count", "appointment_time",
)
QUEUE_KEYS = ("table", "optin_table", "insert_trigger", "update_trigger")


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(
            "Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems)
        )


class Secret:
    """Wraps a credential so it never shows up in logs, reprs or tracebacks."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def get(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('****')"

    __str__ = __repr__


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: Secret


@dataclass(frozen=True)
class AclSettings:
    auth_url: str
    optin_url: str
    send_url: str
    client_id: str
    username: str
    password: Secret


@dataclass(frozen=True)
class BunnySettings:
    storage_host: str
    storage_zone: str
    access_key: Secret
    cdn_url: str
    entry_folder: str
    exit_folder: str


@dataclass(frozen=True)
class WorkerSettings:
    poll_interval_seconds: int
    max_attempts: int
    retry_backoff_minutes: tuple[int, ...]
    stuck_job_minutes: int
    default_country_code: str
    http_timeout_seconds: int
    max_consecutive_db_failures: int
    pdf_output_dir: Path | None
    log_level: str
    log_format: str
    heartbeat_file: Path
    send_enabled: bool


@dataclass(frozen=True)
class TemplateParameter:
    type: str
    source: str


@dataclass(frozen=True)
class TemplateComponent:
    type: str
    parameters: tuple[TemplateParameter, ...]
    sub_type: str | None = None
    index: str | None = None


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    language: str
    language_policy: str
    components: tuple[TemplateComponent, ...]


@dataclass(frozen=True)
class Mapping:
    source: dict[str, str]
    details_json_keys: dict[str, str]
    queue: dict[str, str]
    service_user: str
    service_user_host: str


@dataclass(frozen=True)
class Settings:
    db: DatabaseSettings
    acl: AclSettings
    bunny: BunnySettings
    worker: WorkerSettings
    template: TemplateSpec
    mapping: Mapping


class _Collector:
    """Reads values and records problems instead of raising on the first one."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def env(self, name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            self.problems.append(f"{name} is missing or empty")
        return value

    def env_optional(self, name: str, default: str = "") -> str:
        return os.environ.get(name, default).strip()

    def env_int(self, name: str, minimum: int, maximum: int) -> int:
        raw = self.env(name)
        if not raw:
            return 0
        try:
            value = int(raw)
        except ValueError:
            self.problems.append(f"{name} must be a whole number, got {raw!r}")
            return 0
        if not minimum <= value <= maximum:
            self.problems.append(f"{name} must be between {minimum} and {maximum}, got {value}")
        return value

    def env_bool(self, name: str) -> bool:
        raw = self.env(name).lower()
        if raw in ("true", "1", "yes", "on"):
            return True
        if raw in ("false", "0", "no", "off"):
            return False
        if raw:
            self.problems.append(f"{name} must be true or false, got {raw!r}")
        return False

    def env_https_url(self, name: str) -> str:
        raw = self.env(name)
        if raw:
            parsed = urlparse(raw)
            if parsed.scheme != "https" or not parsed.netloc:
                self.problems.append(f"{name} must be an https:// URL")
        return raw.rstrip("/")


def _read_yaml(path: Path, c: _Collector) -> dict[str, Any]:
    if not path.is_file():
        c.problems.append(f"{path.name} not found at {path}")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        c.problems.append(f"{path.name} is not valid YAML: {exc}")
        return {}
    if not isinstance(data, dict):
        c.problems.append(f"{path.name} must be a mapping at the top level")
        return {}
    return data


def _names(section: Any, keys: tuple[str, ...], where: str, pattern: re.Pattern,
           c: _Collector) -> dict[str, str]:
    if not isinstance(section, dict):
        c.problems.append(f"{where} section is missing")
        return {}
    out: dict[str, str] = {}
    for key in keys:
        value = section.get(key)
        if value is None or str(value).strip() == "":
            c.problems.append(f"{where}.{key} is missing")
            continue
        value = str(value).strip()
        if not pattern.match(value):
            c.problems.append(f"{where}.{key} has invalid characters: {value!r}")
        out[key] = value
    return out


def _load_mapping(path: Path, c: _Collector) -> Mapping:
    data = _read_yaml(path, c)
    source = _names(data.get("source"), SOURCE_KEYS, "source", _IDENTIFIER, c)
    json_keys = _names(data.get("details_json_keys"), DETAILS_JSON_KEYS,
                       "details_json_keys", _JSON_KEY, c)
    queue = _names(data.get("queue"), QUEUE_KEYS, "queue", _IDENTIFIER, c)

    user = data.get("service_user") or {}
    user_name = str(user.get("name", "")).strip()
    user_host = str(user.get("host", "")).strip()
    if not _IDENTIFIER.match(user_name):
        c.problems.append("service_user.name is missing or invalid")
    if not user_host or not re.match(r"^[A-Za-z0-9.%_-]+$", user_host):
        c.problems.append("service_user.host is missing or invalid")

    return Mapping(source, json_keys, queue, user_name, user_host)


def _load_template(path: Path, name: str, language: str, c: _Collector) -> TemplateSpec | None:
    data = _read_yaml(path, c)
    templates = data.get("templates")
    if not isinstance(templates, dict) or not templates:
        c.problems.append(f"{path.name}: 'templates' section is missing or empty")
        return None
    if not name:
        return None
    spec = templates.get(name)
    if not isinstance(spec, dict):
        c.problems.append(
            f"TEMPLATE_NAME={name!r} is not defined in {path.name} "
            f"(defined: {', '.join(sorted(templates))})"
        )
        return None

    where = f"{path.name}: {name}"
    components: list[TemplateComponent] = []
    raw_components = spec.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        c.problems.append(f"{where}: 'components' must be a non-empty list")
        raw_components = []

    for i, comp in enumerate(raw_components):
        cw = f"{where}.components[{i}]"
        if not isinstance(comp, dict):
            c.problems.append(f"{cw} must be a mapping")
            continue
        ctype = comp.get("type")
        if ctype not in COMPONENT_TYPES:
            c.problems.append(f"{cw}.type must be one of {sorted(COMPONENT_TYPES)}")
        sub_type = comp.get("sub_type")
        index = comp.get("index")
        if ctype == "button" and (not sub_type or index is None):
            c.problems.append(f"{cw}: a button needs 'sub_type' and 'index'")

        params: list[TemplateParameter] = []
        raw_params = comp.get("parameters") or []
        if not isinstance(raw_params, list):
            c.problems.append(f"{cw}.parameters must be a list")
            raw_params = []
        for j, p in enumerate(raw_params):
            pw = f"{cw}.parameters[{j}]"
            if not isinstance(p, dict):
                c.problems.append(f"{pw} must be a mapping")
                continue
            if p.get("type") != "text":
                c.problems.append(f"{pw}.type must be 'text'")
            if p.get("source") not in PARAM_SOURCES:
                c.problems.append(f"{pw}.source must be one of {sorted(PARAM_SOURCES)}")
            params.append(TemplateParameter(str(p.get("type")), str(p.get("source"))))

        components.append(TemplateComponent(
            type=str(ctype),
            parameters=tuple(params),
            sub_type=str(sub_type) if sub_type else None,
            index=str(index) if index is not None else None,
        ))

    return TemplateSpec(
        name=name,
        language=language,
        language_policy=str(spec.get("language_policy") or "deterministic"),
        components=tuple(components),
    )


def _parse_backoff(raw: str, c: _Collector) -> tuple[int, ...]:
    if not raw:
        return ()
    try:
        values = tuple(int(v) for v in raw.split(",") if v.strip())
    except ValueError:
        c.problems.append(f"RETRY_BACKOFF_MINUTES must be comma-separated whole numbers, got {raw!r}")
        return ()
    if not values or any(v < 1 for v in values):
        c.problems.append("RETRY_BACKOFF_MINUTES needs at least one value, each >= 1")
    return values


def load_settings(
    env_file: Path | None = PROJECT_ROOT / ".env",
    mapping_file: Path = PROJECT_ROOT / "table_mapping.yaml",
    templates_file: Path = PROJECT_ROOT / "whatsapp_templates.yaml",
) -> Settings:
    """Build Settings or raise ConfigError listing every problem found."""
    if env_file is not None and env_file.is_file():
        # Real environment variables (e.g. from docker compose) win over the file.
        load_dotenv(env_file, override=False)

    c = _Collector()

    db = DatabaseSettings(
        host=c.env("DB_HOST"),
        port=c.env_int("DB_PORT", 1, 65535),
        name=c.env("DB_NAME"),
        user=c.env("DB_USER"),
        password=Secret(c.env("DB_PASSWORD")),
    )

    acl = AclSettings(
        auth_url=c.env_https_url("ACL_AUTH_URL"),
        optin_url=c.env_https_url("ACL_OPTIN_URL"),
        send_url=c.env_https_url("ACL_SEND_URL"),
        client_id=c.env("ACL_CLIENT_ID"),
        username=c.env("ACL_USERNAME"),
        password=Secret(c.env("ACL_PASSWORD")),
    )

    storage_host = c.env("BUNNY_STORAGE_HOST")
    if storage_host and not _HOSTNAME.match(storage_host):
        c.problems.append(
            "BUNNY_STORAGE_HOST must be a hostname only, e.g. storage.bunnycdn.com "
            "(no https://, no zone name)"
        )
    storage_zone = c.env("BUNNY_STORAGE_ZONE")
    if storage_zone and not _FOLDER.match(storage_zone):
        c.problems.append("BUNNY_STORAGE_ZONE has invalid characters")
    folders = {}
    for key in ("BUNNY_ENTRY_FOLDER", "BUNNY_EXIT_FOLDER"):
        folders[key] = c.env(key)
        if folders[key] and not _FOLDER.match(folders[key]):
            c.problems.append(f"{key} must be a folder name without leading/trailing '/'")
    bunny = BunnySettings(
        storage_host=storage_host,
        storage_zone=storage_zone,
        access_key=Secret(c.env("BUNNY_ACCESS_KEY")),
        cdn_url=c.env_https_url("BUNNY_CDN_URL"),
        entry_folder=folders["BUNNY_ENTRY_FOLDER"],
        exit_folder=folders["BUNNY_EXIT_FOLDER"],
    )

    country_code = c.env("DEFAULT_COUNTRY_CODE")
    if country_code and not re.match(r"^[1-9][0-9]{0,2}$", country_code):
        c.problems.append("DEFAULT_COUNTRY_CODE must be 1-3 digits without '+', e.g. 91")

    log_format = c.env_optional("LOG_FORMAT", "pretty").lower() or "pretty"
    if log_format not in ("pretty", "line"):
        c.problems.append("LOG_FORMAT must be 'pretty' or 'line'")

    log_level = c.env("LOG_LEVEL").upper()
    if log_level and log_level not in LOG_LEVELS:
        c.problems.append(f"LOG_LEVEL must be one of {sorted(LOG_LEVELS)}")

    pdf_dir_raw = c.env_optional("PDF_OUTPUT_DIR")
    worker = WorkerSettings(
        poll_interval_seconds=c.env_int("POLL_INTERVAL_SECONDS", 1, 3600),
        max_attempts=c.env_int("MAX_ATTEMPTS", 1, 50),
        retry_backoff_minutes=_parse_backoff(c.env("RETRY_BACKOFF_MINUTES"), c),
        stuck_job_minutes=c.env_int("STUCK_JOB_MINUTES", 1, 1440),
        default_country_code=country_code,
        http_timeout_seconds=c.env_int("HTTP_TIMEOUT_SECONDS", 1, 300),
        max_consecutive_db_failures=c.env_int("MAX_CONSECUTIVE_DB_FAILURES", 1, 10000),
        pdf_output_dir=Path(pdf_dir_raw) if pdf_dir_raw else None,
        log_level=log_level,
        log_format=log_format,
        heartbeat_file=Path(c.env_optional("HEARTBEAT_FILE", "heartbeat")),
        send_enabled=c.env_bool("WHATSAPP_SEND_ENABLED"),
    )

    template_name = c.env("TEMPLATE_NAME")
    template_language = c.env("TEMPLATE_LANGUAGE")
    template = _load_template(templates_file, template_name, template_language, c)
    mapping = _load_mapping(mapping_file, c)

    if c.problems:
        raise ConfigError(c.problems)

    assert template is not None  # guaranteed when there are no problems
    return Settings(db=db, acl=acl, bunny=bunny, worker=worker,
                    template=template, mapping=mapping)


def _summary(s: Settings) -> str:
    def masked(secret: Secret) -> str:
        return "set (****)" if secret.get() else "EMPTY"

    lines = [
        "Configuration OK",
        f"  database   {s.db.user}@{s.db.host}:{s.db.port}/{s.db.name}  password {masked(s.db.password)}",
        f"  acl        client_id set, username set, password {masked(s.acl.password)}",
        f"  template   {s.template.name} ({s.template.language}), "
        f"{sum(len(c.parameters) for c in s.template.components)} parameter(s)",
        f"  bunny      {s.bunny.storage_host}/{s.bunny.storage_zone}  key {masked(s.bunny.access_key)}",
        f"             {s.bunny.cdn_url}/{s.bunny.entry_folder}/ and /{s.bunny.exit_folder}/",
        f"  source     {s.mapping.source['table']} -> queue {s.mapping.queue['table']}",
        f"  worker     poll {s.worker.poll_interval_seconds}s, {s.worker.max_attempts} attempts, "
        f"backoff {list(s.worker.retry_backoff_minutes)} min, stuck after {s.worker.stuck_job_minutes} min",
        f"  sending    {'ENABLED' if s.worker.send_enabled else 'DISABLED (jobs wait as PENDING)'}",
    ]
    return "\n".join(lines)


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(_summary(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
