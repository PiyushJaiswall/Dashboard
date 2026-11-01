"""bcrypt administrator authentication with server-side session state."""
from __future__ import annotations
import hmac, logging, os, re, time
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, MutableMapping, Optional

log = logging.getLogger("stackwealth.auth")
_BCRYPT = re.compile(r"^\$2[abxy]\$\d{2}\$[./A-Za-z0-9]{53}$")
SESSION_TTL_SECONDS = 8 * 3600


class AuthResult(Enum):
    OK = "ok"
    INVALID = "invalid_credentials"
    CONFIG_MISSING = "config_missing"
    CONFIG_BAD_HASH = "config_bad_hash"


@dataclass(frozen=True)
class AuthConfig:
    username: str
    password_hash: str


def is_valid_bcrypt_hash(h: object) -> bool:
    return isinstance(h, str) and bool(_BCRYPT.match(h.strip()))


def load_auth_config(secrets: Optional[Mapping] = None) -> Optional[AuthConfig]:
    """Reads [auth] username/password_hash from secrets, else ADMIN_USERNAME/ADMIN_PASSWORD_HASH env."""
    u = h = None
    try:
        sec = (secrets or {}).get("auth", {}) if secrets is not None else {}
        u, h = sec.get("username"), sec.get("password_hash")
    except Exception:
        pass
    u = u or os.environ.get("ADMIN_USERNAME")
    h = h or os.environ.get("ADMIN_PASSWORD_HASH")
    if not u or not h:
        return None
    return AuthConfig(str(u), str(h).strip())


def hash_password(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_login(username: str, password: str, cfg: Optional[AuthConfig]) -> AuthResult:
    if cfg is None:
        log.error("AUTH CONFIG ERROR: admin username/password_hash not configured")
        return AuthResult.CONFIG_MISSING
    if not is_valid_bcrypt_hash(cfg.password_hash):
        log.error("AUTH CONFIG ERROR: configured password_hash is not a valid bcrypt hash")
        return AuthResult.CONFIG_BAD_HASH
    import bcrypt
    try:
        pw_ok = bcrypt.checkpw((password or "").encode()[:72], cfg.password_hash.encode())
    except ValueError:
        log.error("AUTH CONFIG ERROR: bcrypt rejected configured hash")
        return AuthResult.CONFIG_BAD_HASH
    user_ok = hmac.compare_digest((username or "").encode(), cfg.username.encode())
    if user_ok and pw_ok:
        return AuthResult.OK
    log.warning("AUTH FAILURE: invalid credentials attempt")
    return AuthResult.INVALID


def login(state: MutableMapping, username: str, password: str, cfg: Optional[AuthConfig]) -> AuthResult:
    r = verify_login(username, password, cfg)
    if r is AuthResult.OK:
        state.clear()
        state["sw_auth"] = True
        state["sw_user"] = cfg.username
        state["sw_login_at"] = time.time()
    return r


def is_authenticated(state: Mapping, now: Optional[float] = None) -> bool:
    if not state.get("sw_auth"):
        return False
    return ((now or time.time()) - state.get("sw_login_at", 0)) < SESSION_TTL_SECONDS


def logout(state: MutableMapping) -> None:
    state.clear()


def require_auth(state: MutableMapping) -> None:
    """Call at the top of every data-touching page/export; raises if not authenticated."""
    if not is_authenticated(state):
        state.clear()
        raise PermissionError("Authentication required.")


if __name__ == "__main__":
    import getpass
    print(hash_password(getpass.getpass("New admin password: ")))
