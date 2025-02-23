# mautrix-signal - A Matrix-Signal puppeting bridge
# Copyright (C) 2021 Sumner Evans
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, List, Optional

from attr import dataclass
import asyncpg

from mautrix.types import EventID, RoomID
from mautrix.util.async_db import Database

fake_db = Database.create("") if TYPE_CHECKING else None


@dataclass
class DisappearingMessage:
    db: ClassVar[Database] = fake_db

    room_id: RoomID
    mxid: EventID
    expiration_seconds: int
    expiration_ts: Optional[int] = None

    async def insert(self) -> None:
        q = """
        INSERT INTO disappearing_message (room_id, mxid, expiration_seconds, expiration_ts)
        VALUES ($1, $2, $3, $4)
        """
        await self.db.execute(
            q, self.room_id, self.mxid, self.expiration_seconds, self.expiration_ts
        )

    async def update(self) -> None:
        q = """
        UPDATE disappearing_message
        SET expiration_seconds=$3, expiration_ts=$4
        WHERE room_id=$1 AND mxid=$2
        """
        try:
            await self.db.execute(
                q, self.room_id, self.mxid, self.expiration_seconds, self.expiration_ts
            )
        except Exception as e:
            print(e)

    @classmethod
    async def delete(cls, room_id: RoomID, event_id: EventID) -> None:
        q = "DELETE from disappearing_message WHERE room_id=$1 AND mxid=$2"
        await cls.db.execute(q, room_id, event_id)

    @classmethod
    def _from_row(cls, row: asyncpg.Record) -> DisappearingMessage:
        return cls(**row)

    @classmethod
    async def get(cls, room_id: RoomID, event_id: EventID) -> Optional[DisappearingMessage]:
        q = """
        SELECT room_id, mxid, expiration_seconds, expiration_ts
          FROM disappearing_message
         WHERE room_id = $1
           AND mxid = $2
        """
        try:
            return cls._from_row(await cls.db.fetchrow(q, room_id, event_id))
        except Exception:
            return None

    @classmethod
    async def get_all(cls) -> List[DisappearingMessage]:
        q = "SELECT room_id, mxid, expiration_seconds, expiration_ts FROM disappearing_message"
        return [cls._from_row(r) for r in await cls.db.fetch(q)]

    @classmethod
    async def get_all_for_room(cls, room_id: RoomID) -> List[DisappearingMessage]:
        q = """
        SELECT room_id, mxid, expiration_seconds, expiration_ts
          FROM disappearing_message
         WHERE room_id = $1
        """
        return [cls._from_row(r) for r in await cls.db.fetch(q, room_id)]
