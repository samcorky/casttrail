from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from casttrail.catalogue.models import Person, Production
from casttrail.identity.models import ExternalIdentity
from casttrail.watching.models import Watch

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.providers.base import PersonDetail, Production as ProductionData

TMDB = "tmdb"
IMDB = "imdb"


class WatchService:
    """Following people, films and series.

    Every read and every change takes the user's id, so one user can never see or
    remove another's watches. A watch that does not exist and one that belongs to
    someone else look the same to the caller.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Create the service.

        Args:
            session: The database session to work in.
        """
        self._session = session

    async def list_watches(self, user_id: uuid.UUID) -> list[Watch]:
        """Return a user's watches, newest first, with what they point at loaded.

        Args:
            user_id: The user.

        Returns:
            Their watches.
        """
        result = await self._session.scalars(
            select(Watch)
            .where(Watch.user_id == user_id)
            .order_by(Watch.created_at.desc(), Watch.id)
        )
        return list(result.unique())

    async def watched_people(self, user_id: uuid.UUID) -> dict[int, uuid.UUID]:
        """Map the TMDB id of each person a user watches to the watch's id.

        Args:
            user_id: The user.

        Returns:
            TMDB person id to watch id.
        """
        rows = await self._session.execute(
            select(ExternalIdentity.external_id, Watch.id)
            .join(Watch, Watch.person_id == ExternalIdentity.person_id)
            .where(Watch.user_id == user_id, ExternalIdentity.provider == TMDB)
        )
        return {int(external_id): watch_id for external_id, watch_id in rows}

    async def watched_productions(
        self, user_id: uuid.UUID
    ) -> dict[tuple[str, int], uuid.UUID]:
        """Map each film or series a user watches to the watch's id.

        Args:
            user_id: The user.

        Returns:
            ``(media_type, tmdb_id)`` to watch id.
        """
        rows = await self._session.execute(
            select(Production.media_type, Production.tmdb_id, Watch.id)
            .join(Watch, Watch.production_id == Production.id)
            .where(Watch.user_id == user_id)
        )
        return {
            (media_type, tmdb_id): watch_id for media_type, tmdb_id, watch_id in rows
        }

    async def watch_person(self, user_id: uuid.UUID, data: PersonDetail) -> Watch:
        """Follow a person, adding them to the catalogue if they are new.

        Following someone already followed changes nothing.

        Args:
            user_id: The user following.
            data: The person as the source describes them.

        Returns:
            The watch.
        """
        person = await self._upsert_person(data)
        return await self._watch(user_id, person_id=person.id)

    async def watch_production(self, user_id: uuid.UUID, data: ProductionData) -> Watch:
        """Follow a film or series, adding it to the catalogue if it is new.

        Following something already followed changes nothing.

        Args:
            user_id: The user following.
            data: The film or series as the source describes it.

        Returns:
            The watch.
        """
        production = await self._session.scalar(
            select(Production).where(
                Production.media_type == data.media_type,
                Production.tmdb_id == data.tmdb_id,
            )
        )
        if production is None:
            production = Production(media_type=data.media_type, tmdb_id=data.tmdb_id)
            self._session.add(production)
        production.title = data.title
        production.release_year = data.release_year
        production.poster_path = data.poster_path
        await self._session.flush()
        return await self._watch(user_id, production_id=production.id)

    async def unwatch(self, user_id: uuid.UUID, watch_id: uuid.UUID) -> bool:
        """Stop following something.

        Args:
            user_id: The user. Only their own watches can be removed.
            watch_id: The watch.

        Returns:
            ``True`` if a watch was removed, ``False`` if there is no such watch for
            this user.
        """
        result = await self._session.execute(
            delete(Watch).where(Watch.id == watch_id, Watch.user_id == user_id)
        )
        await self._session.commit()
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def _upsert_person(self, data: PersonDetail) -> Person:
        """Find or create the person for a TMDB id, and record their other ids.

        An identity that already points at a person is never moved, so a link someone
        made by hand is not undone by watching.
        """
        identity = await self._identity(TMDB, str(data.tmdb_id))
        person = (
            await self._session.get(Person, identity.person_id)
            if identity and identity.person_id
            else None
        )
        if person is None:
            person = Person(name=data.name)
            self._session.add(person)
        person.name = data.name
        person.known_for_department = data.known_for_department
        person.profile_path = data.profile_path
        await self._session.flush()
        if identity is None:
            self._session.add(
                ExternalIdentity(
                    provider=TMDB, external_id=str(data.tmdb_id), person_id=person.id
                )
            )
        elif identity.person_id is None:
            identity.person_id = person.id
        if data.imdb_id:
            imdb = await self._identity(IMDB, data.imdb_id)
            if imdb is None:
                self._session.add(
                    ExternalIdentity(
                        provider=IMDB, external_id=data.imdb_id, person_id=person.id
                    )
                )
            elif imdb.person_id is None:
                imdb.person_id = person.id
        await self._session.flush()
        return person

    async def _identity(
        self, provider: str, external_id: str
    ) -> ExternalIdentity | None:
        return await self._session.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == provider,
                ExternalIdentity.external_id == external_id,
            )
        )

    async def _watch(
        self,
        user_id: uuid.UUID,
        *,
        person_id: uuid.UUID | None = None,
        production_id: uuid.UUID | None = None,
    ) -> Watch:
        """Return the user's existing watch for a target, or create it."""
        query = select(Watch).where(
            Watch.user_id == user_id,
            Watch.person_id == person_id
            if person_id is not None
            else Watch.production_id == production_id,
        )
        existing: Watch | None = await self._session.scalar(query)
        if existing is not None:
            await self._session.commit()
            return existing
        watch = Watch(user_id=user_id, person_id=person_id, production_id=production_id)
        self._session.add(watch)
        try:
            await self._session.commit()
        except IntegrityError:
            # Two requests followed the same thing at once; the other one won.
            await self._session.rollback()
            existing = await self._session.scalar(query)
            if existing is None:
                raise
            return existing
        return watch
