"""Migration alignment and explicit refusal of a destructive evidence-policy downgrade."""
import pytest
from alembic import command
from alembic.config import Config
from workmemory.config import ROOT


def test_migration_round_trip_and_model_alignment(database_url):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.check(config)
    with pytest.raises(RuntimeError, match="protects retained history"):
        command.downgrade(config, "53b0f151_obs")
    command.upgrade(config, "head")
    command.check(config)
