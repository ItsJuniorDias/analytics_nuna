"""Configuração lida de variáveis de ambiente.

Sem pydantic-settings nem python-dotenv de propósito: menos dependências num
serviço que só precisa de meia dúzia de valores. O Makefile e o docker-compose
carregam o `.env`; em produção (Render) as variáveis vêm do painel.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "catalog" / "events.json"

# Token de admin curto demais é pior que nenhum: dá falsa sensação de proteção
# num endpoint que expõe o uso agregado do app.
MIN_ADMIN_TOKEN_LENGTH = 16


class ConfigError(ValueError):
    pass


def _split_csv(raw: Optional[str]) -> Tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("%s deve ser inteiro (recebido: %r)" % (name, raw))
    if value < minimum or value > maximum:
        raise ConfigError("%s deve estar entre %d e %d" % (name, minimum, maximum))
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError("%s deve ser true ou false (recebido: %r)" % (name, raw))


@dataclass(frozen=True)
class Settings:
    # Chaves aceitas em X-Nuna-Key. Mais de uma permite rotação: publica a
    # versão do app com a chave nova e só remove a antiga quando ela sumir.
    app_keys: Tuple[str, ...] = ()
    # Sem token, as rotas de estatística respondem 401 para todo mundo.
    admin_token: Optional[str] = None
    db_path: str = "./data/analytics.db"
    catalog_path: str = str(DEFAULT_CATALOG_PATH)
    retention_days: int = 180
    max_batch: int = 100
    max_body_bytes: int = 256 * 1024
    cors_origins: Tuple[str, ...] = ()
    # A fila do app guarda no máximo 7 dias; 30 dá folga para relógio atrasado
    # sem aceitar lixo de anos atrás que distorceria os gráficos.
    max_event_age_days: int = 30
    # Relógio adiantado do aparelho: tolera um dia; além disso o evento ficaria
    # "no futuro" e escaparia da limpeza por data.
    max_clock_skew_hours: int = 24
    # Limpeza automática dentro do próprio serviço. No Render o disco só existe
    # no serviço web, então um cron job separado não enxergaria o SQLite.
    purge_interval_hours: int = 24
    enable_docs: bool = False

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        admin_token = (env.get("NUNA_ADMIN_TOKEN") or "").strip() or None
        if admin_token is not None and len(admin_token) < MIN_ADMIN_TOKEN_LENGTH:
            raise ConfigError(
                "NUNA_ADMIN_TOKEN precisa de pelo menos %d caracteres" % MIN_ADMIN_TOKEN_LENGTH
            )
        return cls(
            app_keys=_split_csv(env.get("NUNA_APP_KEYS")),
            admin_token=admin_token,
            db_path=(env.get("NUNA_DB_PATH") or "./data/analytics.db").strip(),
            catalog_path=(env.get("NUNA_CATALOG_PATH") or str(DEFAULT_CATALOG_PATH)).strip(),
            retention_days=_int(env, "NUNA_RETENTION_DAYS", 180, 1, 3650),
            max_batch=_int(env, "NUNA_MAX_BATCH", 100, 1, 1000),
            max_body_bytes=_int(env, "NUNA_MAX_BODY_BYTES", 256 * 1024, 1024, 5 * 1024 * 1024),
            cors_origins=_split_csv(env.get("NUNA_CORS_ORIGINS")),
            max_event_age_days=_int(env, "NUNA_MAX_EVENT_AGE_DAYS", 30, 1, 36500),
            max_clock_skew_hours=_int(env, "NUNA_MAX_CLOCK_SKEW_HOURS", 24, 0, 24 * 30),
            purge_interval_hours=_int(env, "NUNA_PURGE_INTERVAL_HOURS", 24, 0, 24 * 30),
            enable_docs=_bool(env, "NUNA_ENABLE_DOCS", False),
        )
