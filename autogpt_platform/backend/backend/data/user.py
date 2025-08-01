import base64
import hashlib
import hmac
import logging
from datetime import datetime, timedelta
from typing import Optional, cast
from urllib.parse import quote_plus

from autogpt_libs.auth.models import DEFAULT_USER_ID
from fastapi import HTTPException
from prisma.enums import NotificationType
from prisma.models import User
from prisma.types import JsonFilter, UserCreateInput, UserUpdateInput

from backend.data.db import prisma
from backend.data.model import UserIntegrations, UserMetadata, UserMetadataRaw
from backend.data.notifications import NotificationPreference, NotificationPreferenceDTO
from backend.server.v2.store.exceptions import DatabaseError
from backend.util.encryption import JSONCryptor
from backend.util.json import SafeJson
from backend.util.settings import Settings

logger = logging.getLogger(__name__)


async def get_or_create_user(user_data: dict) -> User:
    try:
        user_id = user_data.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="User ID not found in token")

        user_email = user_data.get("email")
        if not user_email:
            raise HTTPException(status_code=401, detail="Email not found in token")

        user = await prisma.user.find_unique(where={"id": user_id})
        if not user:
            user = await prisma.user.create(
                data=UserCreateInput(
                    id=user_id,
                    email=user_email,
                    name=user_data.get("user_metadata", {}).get("name"),
                )
            )

        return User.model_validate(user)
    except Exception as e:
        raise DatabaseError(f"Failed to get or create user {user_data}: {e}") from e


async def get_user_by_id(user_id: str) -> User:
    user = await prisma.user.find_unique(where={"id": user_id})
    if not user:
        raise ValueError(f"User not found with ID: {user_id}")
    return User.model_validate(user)


async def get_user_email_by_id(user_id: str) -> Optional[str]:
    try:
        user = await prisma.user.find_unique(where={"id": user_id})
        return user.email if user else None
    except Exception as e:
        raise DatabaseError(f"Failed to get user email for user {user_id}: {e}") from e


async def get_user_by_email(email: str) -> Optional[User]:
    try:
        user = await prisma.user.find_unique(where={"email": email})
        return User.model_validate(user) if user else None
    except Exception as e:
        raise DatabaseError(f"Failed to get user by email {email}: {e}") from e


async def update_user_email(user_id: str, email: str):
    try:
        await prisma.user.update(where={"id": user_id}, data={"email": email})
    except Exception as e:
        raise DatabaseError(
            f"Failed to update user email for user {user_id}: {e}"
        ) from e


async def create_default_user() -> Optional[User]:
    user = await prisma.user.find_unique(where={"id": DEFAULT_USER_ID})
    if not user:
        user = await prisma.user.create(
            data=UserCreateInput(
                id=DEFAULT_USER_ID,
                email="default@example.com",
                name="Default User",
            )
        )
    return User.model_validate(user)


async def get_user_metadata(user_id: str) -> UserMetadata:
    user = await User.prisma().find_unique_or_raise(
        where={"id": user_id},
    )

    metadata = cast(UserMetadataRaw, user.metadata)
    return UserMetadata.model_validate(metadata)


async def update_user_metadata(user_id: str, metadata: UserMetadata):
    await User.prisma().update(
        where={"id": user_id},
        data={"metadata": SafeJson(metadata.model_dump())},
    )


async def get_user_integrations(user_id: str) -> UserIntegrations:
    user = await User.prisma().find_unique_or_raise(
        where={"id": user_id},
    )

    encrypted_integrations = user.integrations
    if not encrypted_integrations:
        return UserIntegrations()
    else:
        return UserIntegrations.model_validate(
            JSONCryptor().decrypt(encrypted_integrations)
        )


async def update_user_integrations(user_id: str, data: UserIntegrations):
    encrypted_data = JSONCryptor().encrypt(data.model_dump(exclude_none=True))
    await User.prisma().update(
        where={"id": user_id},
        data={"integrations": encrypted_data},
    )


async def migrate_and_encrypt_user_integrations():
    """Migrate integration credentials and OAuth states from metadata to integrations column."""
    users = await User.prisma().find_many(
        where={
            "metadata": cast(
                JsonFilter,
                {
                    "path": ["integration_credentials"],
                    "not": SafeJson(
                        {"a": "yolo"}
                    ),  # bogus value works to check if key exists
                },
            )
        }
    )
    logger.info(f"Migrating integration credentials for {len(users)} users")

    for user in users:
        raw_metadata = cast(dict, user.metadata)
        metadata = UserMetadata.model_validate(raw_metadata)

        # Get existing integrations data
        integrations = await get_user_integrations(user_id=user.id)

        # Copy credentials and oauth states from metadata if they exist
        if metadata.integration_credentials and not integrations.credentials:
            integrations.credentials = metadata.integration_credentials
        if metadata.integration_oauth_states:
            integrations.oauth_states = metadata.integration_oauth_states

        # Save to integrations column
        await update_user_integrations(user_id=user.id, data=integrations)

        # Remove from metadata
        raw_metadata.pop("integration_credentials", None)
        raw_metadata.pop("integration_oauth_states", None)

        # Update metadata without integration data
        await User.prisma().update(
            where={"id": user.id},
            data={"metadata": SafeJson(raw_metadata)},
        )


async def get_active_user_ids_in_timerange(start_time: str, end_time: str) -> list[str]:
    try:
        users = await User.prisma().find_many(
            where={
                "AgentGraphExecutions": {
                    "some": {
                        "createdAt": {
                            "gte": datetime.fromisoformat(start_time),
                            "lte": datetime.fromisoformat(end_time),
                        }
                    }
                }
            },
        )
        return [user.id for user in users]

    except Exception as e:
        raise DatabaseError(
            f"Failed to get active user ids in timerange {start_time} to {end_time}: {e}"
        ) from e


async def get_active_users_ids() -> list[str]:
    user_ids = await get_active_user_ids_in_timerange(
        (datetime.now() - timedelta(days=30)).isoformat(),
        datetime.now().isoformat(),
    )
    return user_ids


async def get_user_notification_preference(user_id: str) -> NotificationPreference:
    try:
        user = await User.prisma().find_unique_or_raise(
            where={"id": user_id},
        )

        # enable notifications by default if user has no notification preference (shouldn't ever happen though)
        preferences: dict[NotificationType, bool] = {
            NotificationType.AGENT_RUN: user.notifyOnAgentRun or False,
            NotificationType.ZERO_BALANCE: user.notifyOnZeroBalance or False,
            NotificationType.LOW_BALANCE: user.notifyOnLowBalance or False,
            NotificationType.BLOCK_EXECUTION_FAILED: user.notifyOnBlockExecutionFailed
            or False,
            NotificationType.CONTINUOUS_AGENT_ERROR: user.notifyOnContinuousAgentError
            or False,
            NotificationType.DAILY_SUMMARY: user.notifyOnDailySummary or False,
            NotificationType.WEEKLY_SUMMARY: user.notifyOnWeeklySummary or False,
            NotificationType.MONTHLY_SUMMARY: user.notifyOnMonthlySummary or False,
        }
        daily_limit = user.maxEmailsPerDay or 3
        notification_preference = NotificationPreference(
            user_id=user.id,
            email=user.email,
            preferences=preferences,
            daily_limit=daily_limit,
            # TODO with other changes later, for now we just will email them
            emails_sent_today=0,
            last_reset_date=datetime.now(),
        )
        return NotificationPreference.model_validate(notification_preference)

    except Exception as e:
        raise DatabaseError(
            f"Failed to upsert user notification preference for user {user_id}: {e}"
        ) from e


async def update_user_notification_preference(
    user_id: str, data: NotificationPreferenceDTO
) -> NotificationPreference:
    try:
        update_data: UserUpdateInput = {}
        if data.email:
            update_data["email"] = data.email
        if NotificationType.AGENT_RUN in data.preferences:
            update_data["notifyOnAgentRun"] = data.preferences[
                NotificationType.AGENT_RUN
            ]
        if NotificationType.ZERO_BALANCE in data.preferences:
            update_data["notifyOnZeroBalance"] = data.preferences[
                NotificationType.ZERO_BALANCE
            ]
        if NotificationType.LOW_BALANCE in data.preferences:
            update_data["notifyOnLowBalance"] = data.preferences[
                NotificationType.LOW_BALANCE
            ]
        if NotificationType.BLOCK_EXECUTION_FAILED in data.preferences:
            update_data["notifyOnBlockExecutionFailed"] = data.preferences[
                NotificationType.BLOCK_EXECUTION_FAILED
            ]
        if NotificationType.CONTINUOUS_AGENT_ERROR in data.preferences:
            update_data["notifyOnContinuousAgentError"] = data.preferences[
                NotificationType.CONTINUOUS_AGENT_ERROR
            ]
        if NotificationType.DAILY_SUMMARY in data.preferences:
            update_data["notifyOnDailySummary"] = data.preferences[
                NotificationType.DAILY_SUMMARY
            ]
        if NotificationType.WEEKLY_SUMMARY in data.preferences:
            update_data["notifyOnWeeklySummary"] = data.preferences[
                NotificationType.WEEKLY_SUMMARY
            ]
        if NotificationType.MONTHLY_SUMMARY in data.preferences:
            update_data["notifyOnMonthlySummary"] = data.preferences[
                NotificationType.MONTHLY_SUMMARY
            ]
        if data.daily_limit:
            update_data["maxEmailsPerDay"] = data.daily_limit

        user = await User.prisma().update(
            where={"id": user_id},
            data=update_data,
        )
        if not user:
            raise ValueError(f"User not found with ID: {user_id}")
        preferences: dict[NotificationType, bool] = {
            NotificationType.AGENT_RUN: user.notifyOnAgentRun or True,
            NotificationType.ZERO_BALANCE: user.notifyOnZeroBalance or True,
            NotificationType.LOW_BALANCE: user.notifyOnLowBalance or True,
            NotificationType.BLOCK_EXECUTION_FAILED: user.notifyOnBlockExecutionFailed
            or True,
            NotificationType.CONTINUOUS_AGENT_ERROR: user.notifyOnContinuousAgentError
            or True,
            NotificationType.DAILY_SUMMARY: user.notifyOnDailySummary or True,
            NotificationType.WEEKLY_SUMMARY: user.notifyOnWeeklySummary or True,
            NotificationType.MONTHLY_SUMMARY: user.notifyOnMonthlySummary or True,
        }
        notification_preference = NotificationPreference(
            user_id=user.id,
            email=user.email,
            preferences=preferences,
            daily_limit=user.maxEmailsPerDay or 3,
            # TODO with other changes later, for now we just will email them
            emails_sent_today=0,
            last_reset_date=datetime.now(),
        )
        return NotificationPreference.model_validate(notification_preference)

    except Exception as e:
        raise DatabaseError(
            f"Failed to update user notification preference for user {user_id}: {e}"
        ) from e


async def set_user_email_verification(user_id: str, verified: bool) -> None:
    """Set the email verification status for a user."""
    try:
        await User.prisma().update(
            where={"id": user_id},
            data={"emailVerified": verified},
        )
    except Exception as e:
        raise DatabaseError(
            f"Failed to set email verification status for user {user_id}: {e}"
        ) from e


async def get_user_email_verification(user_id: str) -> bool:
    """Get the email verification status for a user."""
    try:
        user = await User.prisma().find_unique_or_raise(
            where={"id": user_id},
        )
        return user.emailVerified
    except Exception as e:
        raise DatabaseError(
            f"Failed to get email verification status for user {user_id}: {e}"
        ) from e


def generate_unsubscribe_link(user_id: str) -> str:
    """Generate a link to unsubscribe from all notifications"""
    # Create an HMAC using a secret key
    secret_key = Settings().secrets.unsubscribe_secret_key
    signature = hmac.new(
        secret_key.encode("utf-8"), user_id.encode("utf-8"), hashlib.sha256
    ).digest()

    # Create a token that combines the user_id and signature
    token = base64.urlsafe_b64encode(
        f"{user_id}:{signature.hex()}".encode("utf-8")
    ).decode("utf-8")
    logger.info(f"Generating unsubscribe link for user {user_id}")

    base_url = Settings().config.platform_base_url
    return f"{base_url}/api/email/unsubscribe?token={quote_plus(token)}"


async def unsubscribe_user_by_token(token: str) -> None:
    """Unsubscribe a user from all notifications using the token"""
    try:
        # Decode the token
        decoded = base64.urlsafe_b64decode(token).decode("utf-8")
        user_id, received_signature_hex = decoded.split(":", 1)

        # Verify the signature
        secret_key = Settings().secrets.unsubscribe_secret_key
        expected_signature = hmac.new(
            secret_key.encode("utf-8"), user_id.encode("utf-8"), hashlib.sha256
        ).digest()

        if not hmac.compare_digest(expected_signature.hex(), received_signature_hex):
            raise ValueError("Invalid token signature")

        user = await get_user_by_id(user_id)
        await update_user_notification_preference(
            user.id,
            NotificationPreferenceDTO(
                email=user.email,
                daily_limit=0,
                preferences={
                    NotificationType.AGENT_RUN: False,
                    NotificationType.ZERO_BALANCE: False,
                    NotificationType.LOW_BALANCE: False,
                    NotificationType.BLOCK_EXECUTION_FAILED: False,
                    NotificationType.CONTINUOUS_AGENT_ERROR: False,
                    NotificationType.DAILY_SUMMARY: False,
                    NotificationType.WEEKLY_SUMMARY: False,
                    NotificationType.MONTHLY_SUMMARY: False,
                },
            ),
        )
    except Exception as e:
        raise DatabaseError(f"Failed to unsubscribe user by token {token}: {e}") from e
