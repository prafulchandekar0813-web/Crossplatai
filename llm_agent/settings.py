"""Live configuration holder: file layer + environment layer, validated hot-reload, admin credentials.

The rest of the app reads `settings.effective` (a fresh object after every change). A running task keeps the
snapshot it started with, so editing settings never changes the rules halfway through a task.
"""
import copy
import logging
import os
import secrets
import threading
from typing import Any, Dict, List, Optional, Set

from llm_agent.config import (
    DEFAULT_SYSTEM_PROMPT,
    SystemConfig,
    apply_env,
    load_file_config,
    public_view,
    save_file_config,
)
from llm_agent.security import hash_password

log = logging.getLogger("llm_agent.settings")


class ConfigError(Exception):
    def __init__(self, problems: List[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


class Settings:
    def __init__(self, config_path: str):
        self.path = os.path.abspath(config_path)
        self._lock = threading.RLock()
        self._file = SystemConfig()
        self._effective = SystemConfig()
        self._locked: Set[str] = set()
        self._env_admin: Optional[tuple] = None  # (plaintext, hash) cache so reloads don't re-run scrypt
        self.generated_admin_password: Optional[str] = None
        self.reload()

    # ---- reading ---------------------------------------------------------------------------------
    @property
    def effective(self) -> SystemConfig:
        return self._effective

    @property
    def locked(self) -> Set[str]:
        return set(self._locked)

    def admin_view(self) -> Dict[str, Any]:
        view = public_view(self._effective, self._locked)
        view["system_prompt_is_default"] = not self._effective.system_prompt.strip()
        return view

    def public(self) -> Dict[str, Any]:
        """What any signed-in (or signed-out) client may know."""
        c = self._effective
        return {
            "llm_ready": bool(c.openai.base_url and c.openai.model),
            "model": c.openai.model,
            "search_enabled": bool(c.search.provider),
            "allow_registration": c.allow_registration,
            "max_prompt_chars": c.max_prompt_chars,
            "max_subagents_per_user": c.max_subagents_per_user,
            "limits": {
                "max_total_turns": c.circuit_breaker.max_total_turns,
                "max_tool_calls": c.circuit_breaker.max_tool_calls,
                "max_run_seconds": c.circuit_breaker.max_run_seconds,
                "max_delegations": c.circuit_breaker.max_delegations,
            },
        }

    # ---- loading ---------------------------------------------------------------------------------
    def reload(self) -> None:
        with self._lock:
            self._file = load_file_config(self.path)
            self._bootstrap_admin_password()
            self._recompute()

    def _recompute(self) -> None:
        eff, locked = apply_env(self._file)
        env_pw = os.environ.get("ADMIN_PASSWORD")
        if env_pw:
            if not self._env_admin or self._env_admin[0] != env_pw:
                self._env_admin = (env_pw, hash_password(env_pw))
            eff.admin.admin_password_hash = self._env_admin[1]
        self._effective, self._locked = eff, locked

    def _bootstrap_admin_password(self) -> None:
        """First start without ADMIN_PASSWORD: generate a random one, store only its hash, show it once."""
        if os.environ.get("ADMIN_PASSWORD") or self._file.admin.admin_password_hash:
            return
        password = secrets.token_urlsafe(12)
        self._file.admin.admin_password_hash = hash_password(password)
        save_file_config(self._file, self.path)
        self.generated_admin_password = password

    # ---- writing ---------------------------------------------------------------------------------
    def update(self, patch: Dict[str, Any]) -> None:
        """Validate and apply an admin edit. Raises ConfigError (nothing is changed) on bad input.

        - the `admin` section cannot be edited here (the password has its own endpoint)
        - an empty/missing api_key keeps the stored key; `null` clears it
        """
        if not isinstance(patch, dict):
            raise ConfigError(["config must be an object"])
        patch = copy.deepcopy(patch)
        patch.pop("admin", None)
        patch.pop("env_locked", None)
        patch.pop("system_prompt_is_default", None)
        for name in ("openai", "search"):
            section = patch.get(name)
            if isinstance(section, dict):
                section.pop("api_key_set", None)
                if section.get("api_key") == "":
                    section.pop("api_key")
        with self._lock:
            data = self._file.to_dict()
            _deep_merge(data, patch)
            try:
                new_file = SystemConfig.from_dict(data, strict=True)
            except ValueError as e:
                raise ConfigError([str(e)])
            if new_file.system_prompt.strip() == DEFAULT_SYSTEM_PROMPT.strip():
                new_file.system_prompt = ""
            eff, _ = apply_env(new_file)
            problems = eff.problems()
            if problems:
                raise ConfigError(problems)
            save_file_config(new_file, self.path)
            self._file = new_file
            self._recompute()

    def reset(self) -> None:
        """Back to defaults, keeping the admin password and any stored API keys."""
        with self._lock:
            fresh = SystemConfig()
            fresh.admin = self._file.admin
            fresh.openai.api_key = self._file.openai.api_key
            fresh.search.api_key = self._file.search.api_key
            save_file_config(fresh, self.path)
            self._file = fresh
            self._recompute()

    def set_admin_password_hash(self, new_hash: str) -> None:
        with self._lock:
            if "admin.admin_password" in self._locked:
                raise ConfigError(["The admin password is set by the ADMIN_PASSWORD environment variable"])
            self._file.admin.admin_password_hash = new_hash
            save_file_config(self._file, self.path)
            self._recompute()
