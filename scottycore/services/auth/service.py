"""Auth service — JWT token management, credential verification, and TOTP 2FA."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import UUID

import pyotp
import qrcode
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from scottycore.core.config import get_settings
from scottycore.core.exceptions import AuthenticationError
from scottycore.services.audit.schemas import AuditLogCreate
from scottycore.services.audit.service import AuditService
from scottycore.services.auth.models import LoginAttempt, RefreshToken
from scottycore.services.auth.schemas import BackupCodesResponse, LoginResponse, TOTPSetupResponse, TokenResponse, UserContext

if TYPE_CHECKING:
    from scottycore.services.users.service import UsersService

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_BACKUP_CODE_COUNT = 8
_TOTP_PARTIAL_TOKEN_MINUTES = 5

# Account lockout policy
_MAX_FAILED_ATTEMPTS = 5        # consecutive failures allowed before lockout
_LOCKOUT_WINDOW_MINUTES = 15    # rolling window to count failures in
_LOCKOUT_DURATION_MINUTES = 30  # how long the account stays locked


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

    def create_access_token(
        self,
        user_id: UUID,
        extra_claims: dict | None = None,
        totp_pending: bool = False,
        session_version: int = 0,
    ) -> str:
        now = datetime.now(timezone.utc)
        expire_minutes = (
            _TOTP_PARTIAL_TOKEN_MINUTES
            if totp_pending
            else self._settings.jwt_access_token_expire_minutes
        )
        expires = now + timedelta(minutes=expire_minutes)
        payload: dict = {
            "sub": str(user_id),
            "type": "access",
            "iat": now,
            "exp": expires,
            "sv": session_version,  # session_version claim for forced re-auth
        }
        if totp_pending:
            payload["totp_pending"] = True
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

    # ── Account lockout ────────────────────────────────────────────────

    async def record_login_attempt(
        self, email: str, success: bool, ip_address: str = "unknown"
    ) -> None:
        """Persist a login attempt record.  Never raises — failures are silent."""
        try:
            async with self._session_factory() as session:
                attempt = LoginAttempt(
                    email=email.lower(),
                    success=success,
                    ip_address=ip_address,
                )
                session.add(attempt)
                await session.commit()
        except Exception:
            pass

    async def check_lockout(self, email: str) -> bool:
        """Return True if the account is currently locked out.

        Locked when there are >= _MAX_FAILED_ATTEMPTS consecutive failures
        within the last _LOCKOUT_WINDOW_MINUTES, and the most recent failure
        is less than _LOCKOUT_DURATION_MINUTES old.

        Implementation: count failures in the rolling window, but stop counting
        when a success is encountered (resets the streak).
        """
        window_start = datetime.now(timezone.utc) - timedelta(minutes=_LOCKOUT_WINDOW_MINUTES)
        lockout_start = datetime.now(timezone.utc) - timedelta(minutes=_LOCKOUT_DURATION_MINUTES)

        async with self._session_factory() as session:
            result = await session.scalars(
                select(LoginAttempt)
                .where(
                    LoginAttempt.email == email.lower(),
                    LoginAttempt.attempted_at >= window_start,
                )
                .order_by(LoginAttempt.attempted_at.desc())
            )
            recent_attempts = result.all()

        # Count consecutive failures from most recent
        consecutive_failures = 0
        for attempt in recent_attempts:
            if attempt.success:
                break  # A success resets the streak
            consecutive_failures += 1

        if consecutive_failures < _MAX_FAILED_ATTEMPTS:
            return False

        # Confirm the most recent failure is still within the lockout duration
        if recent_attempts and recent_attempts[0].attempted_at >= lockout_start:
            return True

        return False

    # ── Login orchestration ─────────────────────────────────────────────

    async def login(
        self,
        email: str,
        password: str,
        ip_address: str,
        users_service: UsersService,
    ) -> LoginResponse:
        """Full login flow: lockout check, credential verification, TOTP branch.

        Args:
            email: Raw email from the request (will be lowered/stripped).
            password: Plaintext password.
            ip_address: Client IP for audit logging.
            users_service: UsersService instance (injected to avoid circular import).

        Raises:
            AuthenticationError: On any failure (generic message to prevent enumeration).
        """
        _GENERIC = "Invalid email or password"
        email_lower = email.strip().lower()

        # Check lockout before attempting credential verification
        if await self.check_lockout(email_lower):
            await self.record_login_attempt(email_lower, success=False, ip_address=ip_address)
            raise AuthenticationError(_GENERIC)

        password_hash = await users_service.get_password_hash(email_lower)  # type: ignore[union-attr]
        if not password_hash or not self.verify_password(password, password_hash):
            await self.record_login_attempt(email_lower, success=False, ip_address=ip_address)
            raise AuthenticationError(_GENERIC)

        user = await users_service.get_by_email(email_lower)  # type: ignore[union-attr]
        if not user or not user.is_active:
            await self.record_login_attempt(email_lower, success=False, ip_address=ip_address)
            raise AuthenticationError(_GENERIC)

        session_version = user.session_version

        await self.record_login_attempt(email_lower, success=True, ip_address=ip_address)
        await self._audit.log(AuditLogCreate(
            user_id=user.id,
            action="auth.login",
            target_type="user",
            target_id=str(user.id),
            detail={"email": email_lower},
            ip_address=ip_address,
        ))

        if user.totp_enabled:
            partial_token = self.create_access_token(
                user.id, totp_pending=True, session_version=session_version
            )
            return LoginResponse(requires_totp=True, partial_token=partial_token)

        access_token = self.create_access_token(user.id, session_version=session_version)
        return LoginResponse(
            access_token=access_token,
            expires_in=self._settings.jwt_access_token_expire_minutes * 60,
        )

    # ── TOTP 2FA ───────────────────────────────────────────────────────

    def generate_totp_secret(self, user_email: str) -> TOTPSetupResponse:
        """Generate a new TOTP secret, otpauth URI, and QR code PNG (base64).

        The secret is returned to the caller; it must be passed back via
        ``enable_totp`` after the user confirms it with their authenticator app.
        The secret is NOT persisted here — only after successful verification.
        """
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        otpauth_uri = totp.provisioning_uri(
            name=user_email,
            issuer_name=self._settings.app_name,
        )
        qr_code_b64 = self._make_qr_base64(otpauth_uri)
        return TOTPSetupResponse(
            secret=secret,
            otpauth_uri=otpauth_uri,
            qr_code_base64=qr_code_b64,
        )

    @staticmethod
    def _make_qr_base64(uri: str) -> str:
        """Render a QR code URI as a base64-encoded PNG data URI."""
        img = qrcode.make(uri, box_size=6, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"

    @staticmethod
    def _verify_totp_code(secret: str, code: str) -> bool:
        """Verify a 6-digit TOTP code with ±1 period clock-drift tolerance."""
        totp = pyotp.TOTP(secret)
        return totp.verify(code.strip(), valid_window=1)

    @staticmethod
    def _generate_plaintext_backup_codes(count: int = _BACKUP_CODE_COUNT) -> list[str]:
        """Return a list of hex backup codes (uppercase, 8 chars each)."""
        return [secrets.token_hex(4).upper() for _ in range(count)]

    @staticmethod
    def _hash_backup_codes(codes: list[str]) -> str:
        """Bcrypt-hash each backup code and serialise to a JSON array string."""
        hashed = [
            pwd_context.hash(c.upper())
            for c in codes
        ]
        return json.dumps(hashed)

    async def enable_totp(
        self,
        user_id: UUID,
        secret: str,
        code: str,
        users_service: UsersService | None = None,
    ) -> BackupCodesResponse:
        """Validate *code* against *secret*, then persist the secret and generate backup codes.

        Raises ``AuthenticationError`` if the code is invalid.
        User mutations are delegated to UsersService.
        """
        if not self._verify_totp_code(secret, code):
            raise AuthenticationError("Invalid TOTP code — please try again")

        plaintext_codes = self._generate_plaintext_backup_codes()
        hashed_json = self._hash_backup_codes(plaintext_codes)

        if users_service is None:
            raise AuthenticationError("Internal error: users_service required")
        await users_service.set_totp(user_id, secret, hashed_json)

        return BackupCodesResponse(codes=plaintext_codes)

    async def disable_totp(
        self,
        user_id: UUID,
        users_service: UsersService | None = None,
    ) -> None:
        """Clear all TOTP fields for the given user.

        User mutations are delegated to UsersService.
        """
        if users_service is None:
            raise AuthenticationError("Internal error: users_service required")
        await users_service.clear_totp(user_id)

    async def verify_totp(
        self,
        user_id: UUID,
        code: str,
        users_service: UsersService | None = None,
    ) -> bool:
        """Verify a TOTP or backup code for the given user.

        Backup codes are one-time-use: a matching code is removed from the
        stored list on success.  Returns ``True`` on success.
        User data is read/written via UsersService.
        """
        if users_service is None:
            return False

        totp_secret, totp_enabled, backup_codes = await users_service.get_totp_fields(user_id)
        if not totp_enabled or not totp_secret:
            return False

        # Check TOTP first
        if self._verify_totp_code(totp_secret, code):
            return True

        # Fall back to backup code
        if not backup_codes:
            return False
        try:
            hashed_list: list[str] = json.loads(backup_codes)
        except (json.JSONDecodeError, TypeError):
            return False

        normalized = code.strip().upper()
        for i, h in enumerate(hashed_list):
            if pwd_context.verify(normalized, h):
                # Consume the code — remove from list
                hashed_list.pop(i)
                await users_service.consume_backup_code(
                    user_id, json.dumps(hashed_list)
                )
                return True

        return False
