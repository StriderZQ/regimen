"""Domain models. Imported here so Alembic sees every table."""

from app.models.base import Base, DomainBase, new_id, utcnow
from app.models.catalog import (
    CanonicalProduct,
    IngredientAlias,
    ItemAlias,
    NutritionSource,
    StoreItem,
    Unit,
)
from app.models.diary import (
    Confidence,
    ExternalKind,
    ExternalMeal,
    ItemOrigin,
    Meal,
    MealItem,
    MealSource,
)
from app.models.garmin import GarminKind, GarminMetric, GarminRaw
from app.models.identity import Client, ClientKind, Grant, Principal, Scope
from app.models.oauth import (
    AuthRequestStatus,
    OAuthAuthRequest,
    OAuthClient,
    OAuthToken,
)
from app.models.ops import (
    CallLog,
    IdempotencyKey,
    Job,
    JobStatus,
)
from app.models.pantry import (
    Batch,
    BatchSource,
    ExpirySource,
    MoveKind,
    PreparationRun,
    StockMove,
    StoragePlace,
)
from app.models.policies import (
    BodyMeasurement,
    Policy,
    PolicyKind,
    Reminder,
    ShelfLifeRule,
    Supplement,
    TypicalWeight,
)
from app.models.receipts import LineStatus, Receipt, ReceiptLine
from app.models.recipes import Recipe, RecipeIngredient, RecipeSource
from app.models.training import (
    Equipment,
    Exercise,
    ProgramDay,
    SessionSource,
    SetLog,
    WorkoutSession,
)

__all__ = [
    "Base", "Batch", "BatchSource", "CallLog", "CanonicalProduct",
    "Equipment", "Exercise", "GarminKind", "GarminMetric", "GarminRaw", "ProgramDay",
    "SessionSource", "SetLog", "WorkoutSession",
    "Client", "ClientKind", "Confidence", "DomainBase", "ExpirySource", "ExternalKind",
    "IngredientAlias",
    "ExternalMeal", "Grant", "IdempotencyKey", "ItemAlias", "ItemOrigin", "Job",
    "JobStatus", "LineStatus", "Meal", "MealItem", "MealSource", "MoveKind",
    "NutritionSource", "AuthRequestStatus", "OAuthAuthRequest", "OAuthClient",
    "OAuthToken", "BodyMeasurement", "Policy", "PolicyKind", "PreparationRun",
    "Principal", "Receipt", "ReceiptLine", "Recipe", "RecipeIngredient", "RecipeSource",
    "Reminder",
    "Scope", "ShelfLifeRule", "StockMove", "StoragePlace", "StoreItem",
    "Supplement", "TypicalWeight", "Unit", "new_id", "utcnow",
]
