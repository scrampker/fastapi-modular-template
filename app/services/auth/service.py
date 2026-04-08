"""Auth service — JWT token management and credential verification."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.services.audit.schemas import AuditLogCreate
from app.services.audit.service import AuditService
from app.services.auth.models import RefreshToken
from app.services.auth.schemas import TokenResponse, UserContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AuthService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditService,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._settings = get_settings()

    # ── Password hashing ───────────────────────────────────────────

    @staticmethod
    def hash_password(password: str) -> str:
        return pwd_context.hash(password)

    @staticmethod
    def verify_password(plain: str, hashed: str) -> bool:
        return pwd_context.verify(plain, hashed)

    # ── API key hashing ────────────────────────────────────────────

    @staticmethod
    def hash_api_key(key: str) -> str:
        return pwd_context.hash(key)

    @staticmethod
    def verify_api_key(plain: str, hashed: str) -> bool:
        return pwd_context.verify(plain, hashed)

    # ── JWT tokens ─────────────────────────────────────────────────

    def create_access_token(self, user_id: UUID, extra_claims: dict | None = None) -> str:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=self._settings.jwt_access_token_expire_minutes)
        payload = {
            "sub": str(user_id),
            "type": "access",
            "iat": now,
            "exp": expires,
        }
        if extra_claims:
            payload.update(extra_claims)
        return jwt.encode(payload, self._settings.jwt_secret_key, algorithm=self._settings.jwt_algorithm)

    def create_refresh_token_value(self, user_id: UUID) -> tuple[str, datetime]:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=self._settings.jwt_refresh_token_expire_days)
        payload = {
            "sub": str(user_id),
            "type": "refresh",
            "iat": now,
            "exp": expires,
        }
        token = jwt.encode(payload, self._settings.jwt_secret_key, algorithm=self._settings.jwt_algorithm)
        return token, expires

    def decode_token(self, token: str) -> dict:
        """Decode and validate a JWT. Raises AuthenticationError on failure."""
        try:
            payload = jwt.decode(
                token,
                self._settings.jwt_secret_key,
                algorithms=[self._settings.jwt_algorithm],
            )
            return payload
        except JWTError as e:
            raise AuthenticationError(f"Invalid token: {e}") from e

    # ── Refresh token persistence ──────────────────────────────────

    @staticmethod
    def _token_prefix(token: str) -> str:
        """Compute a 16-char SHA-256 prefix for fast token lookup."""
        return hashlib.sha256(token.encode()).hexdigest()[:16]

    async def store_refresh_token(self, user_id: UUID, token: str, expires_at: datetime) -> None:
        token_hash = self.hash_password(token)  # reuse bcrypt for token hashing
        token_prefix = self._token_prefix(token)
        async with self._session_factory() as session:
            record = RefreshToken(
                user_id=str(user_id),
                token_hash=token_hash,
                token_prefix=token_prefix,
                expires_at=expires_at,
            )
            session.add(record)
            await session.commit()

    async def revoke_refresh_token(self, token: str) -> None:
        """Revoke a refresh token by marking it in the DB."""
        prefix = self._token_prefix(token)
        async with self._session_factory() as session:
            result = await session.scalars(
                select(RefreshToken).where(
                    RefreshToken.token_prefix == prefix,
                    RefreshToken.is_revoked == False,  # noqa: E712
                )
            )
            rt = result.first()
            if rt and self.verify_password(token, rt.token_hash):
                rt.is_revoked = True
                await session.commit()

    async def is_refresh_token_valid(self, token: str) -> bool:
        """Check if a refresh token exists, is not revoked, and not expired."""
        prefix = self._token_prefix(token)
        async with self._session_factory() as session:
            result = await session.scalars(
                select(RefreshToken).where(
                    RefreshToken.token_prefix == prefix,
                    RefreshToken.is_revoked == False,  # noqa: E712
                    RefreshToken.expires_at > datetime.now(timezone.utc),
                )
            )
            rt = result.first()
            if rt and self.verify_password(token, rt.token_hash):
                return True
            return False

    def build_token_response(self, access_token: str) -> TokenResponse:
        return TokenResponse(
            access_token=access_token,
            expires_in=self._settings.jwt_access_token_expire_minutes * 60,
        )
