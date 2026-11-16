import uuid

from casttrail.notifications.models import event_key_for


def test_the_event_key_names_the_convention_and_the_person() -> None:
    convention = uuid.UUID(int=1)
    person = uuid.UUID(int=2)

    assert event_key_for(convention, person) == f"appear:{convention}:{person}"
