import pytest

import bacc.translations


@pytest.fixture(autouse=True)
def no_packaged_name_overrides(monkeypatch):
    """The shipped entries name file IDs the synthetic data lacks, so each would add a row."""
    monkeypatch.setattr(bacc.translations, 'packaged_name_overrides', dict)
