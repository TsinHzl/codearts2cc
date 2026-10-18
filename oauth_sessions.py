# -*- coding: utf-8 -*-
"""浏览器登录会话仓库（design D6）：纯内存、RLock、读写前惰性 GC。"""
import dataclasses
import hmac
import secrets
import threading
import time

from oauth_protocol import build_authorize_url, generate_pkce, new_dpop_private_jwk

SESSION_TTL_SECONDS = 15 * 60   # 登录会话 TTL
SESSION_MAX_TOTAL = 20          # 会话总数上限（含 done，直到 GC 清除）
SESSION_MAX_PER_ADMIN = 5       # 单管理会话活跃登录上限
SESSION_MAX_CANDIDATES = 10     # 无判别键时候选上限


class SessionLimitError(Exception):
    """登录会话总数或单管理会话活跃数超限（调用方返回 409）。"""


class SessionUnavailableError(Exception):
    """会话不存在、已过期或不属于当前管理会话（调用方返回 409）。"""


def _digest_equal(stored: object, supplied: object) -> bool:
    """恒定时间比较两个判别凭据；非 str 输入直接判否。

    统一 UTF-8 编码为 bytes 再比较，避免非 ASCII str 触发
    UnicodeEncodeError、bytes/int 触发 TypeError 导致端点 500。
    """
    if not isinstance(stored, str) or not isinstance(supplied, str):
        return False
    try:
        stored_bytes = stored.encode("utf-8")
        supplied_bytes = supplied.encode("utf-8")
    except UnicodeEncodeError:  # 孤立代理字符（如 "\ud800"）无法编码
        return False
    return hmac.compare_digest(stored_bytes, supplied_bytes)


@dataclasses.dataclass
class OAuthSession:
    """单次浏览器登录的会话状态（仅内存，随进程消失）。"""

    id: str
    ticket_id: str
    secret: str
    verifier: str
    dpop_private_key: dict
    port: int
    auth_url: str
    state: str
    admin_session: str
    created_at: float
    done: bool = False
    err: str = ""
    ticket_fallback: bool = False


class OAuthSessionStore:
    """纯内存会话仓库：dict + RLock，读写前惰性 GC。

    ticket 请求由 poll 端点在 ticket_fallback 为真时发起一次（无循环），
    「每次 poll 最多 1 次 ticket 请求」由调用方结构保证。
    """

    def __init__(self, clock=time.time) -> None:
        self._sessions: dict[str, OAuthSession] = {}
        self._lock = threading.RLock()
        self._clock = clock

    def _gc_locked(self) -> None:
        now = self._clock()
        expired = [sid for sid, s in self._sessions.items()
                   if now - s.created_at > SESSION_TTL_SECONDS]
        for sid in expired:
            del self._sessions[sid]

    def create(self, port: int, admin_session: str) -> OAuthSession:
        """创建登录会话（生成 ticket_id/secret/PKCE/DPoP/state 与授权链接）。

        超出总数或单管理会话上限抛 SessionLimitError（409）。
        """
        ticket_id = secrets.token_hex(16)
        secret = secrets.token_hex(16)
        verifier, challenge = generate_pkce()
        state = secrets.token_hex(16)
        session = OAuthSession(
            id=secrets.token_hex(16), ticket_id=ticket_id, secret=secret,
            verifier=verifier, dpop_private_key=new_dpop_private_jwk(),
            port=port, auth_url=build_authorize_url(ticket_id, challenge, state, port),
            state=state, admin_session=admin_session, created_at=self._clock(),
        )
        with self._lock:
            self._gc_locked()
            if len(self._sessions) >= SESSION_MAX_TOTAL:
                raise SessionLimitError("oauth session limit reached")
            active = sum(1 for s in self._sessions.values()
                         if not s.done and s.admin_session == admin_session)
            if active >= SESSION_MAX_PER_ADMIN:
                raise SessionLimitError("admin session login limit reached")
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> OAuthSession | None:
        with self._lock:
            self._gc_locked()
            return self._sessions.get(session_id)

    def require(self, session_id: str, admin_session: str) -> OAuthSession:
        """校验会话存在且属于当前管理会话，否则抛 SessionUnavailableError。"""
        with self._lock:
            self._gc_locked()
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionUnavailableError("session not found or expired")
            if session.admin_session != admin_session:
                raise SessionUnavailableError("session belongs to another admin session")
            return session

    def enable_ticket_fallback(self, session_id: str, secret: str) -> bool:
        """首次 secret 回调：更新会话 secret 并置 ticket_fallback（开启 ticket 轮询门控）。

        仅首次调用生效：空 secret、会话已结束或已开启兜底时返回 False。
        """
        if not secret:
            return False
        with self._lock:
            self._gc_locked()
            session = self._sessions.get(session_id)
            if session is None or session.done or session.ticket_fallback:
                return False
            session.secret = secret
            session.ticket_fallback = True
            return True

    def candidates(self, ticket_id: str = "", secret: str = "") -> list[OAuthSession]:
        """候选顺序：secret 命中 → ticket_id 命中 → 活跃会话 created_at 倒序（上限 10）。

        带 secret / ticket_id 但不命中时返回空列表，不再猜测（design D6）。
        """
        with self._lock:
            self._gc_locked()
            # 判别凭据用恒定时间比对（同 api_keys 先例）；带判别键未命中即空，不猜
            for field, value in (("secret", secret), ("ticket_id", ticket_id)):
                if not value:
                    continue
                for session in self._sessions.values():
                    if _digest_equal(getattr(session, field), value) \
                            and not session.done:
                        return [session]
                return []
            active = [s for s in self._sessions.values() if not s.done]
            active.sort(key=lambda s: s.created_at, reverse=True)
            return active[:SESSION_MAX_CANDIDATES]

    def complete(self, session_id: str, err: str = "") -> None:
        """结束会话；err 非空表示登录失败原因（不含敏感值）。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.done = True
                session.err = err

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
