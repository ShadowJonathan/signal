# Copyright (c) 2020 Tulir Asokan
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Type, TypeVar, Union
import asyncio

from mautrix.util.logging import TraceLogger

from .errors import UnexpectedError, UnexpectedResponse
from .rpc import CONNECT_EVENT, DISCONNECT_EVENT, SignaldRPCClient
from .types import (
    Account,
    Address,
    Attachment,
    DeviceInfo,
    GetIdentitiesResponse,
    Group,
    GroupID,
    GroupV2,
    LinkSession,
    Mention,
    Message,
    Profile,
    Quote,
    Reaction,
    WebsocketConnectionState,
    WebsocketConnectionStateChangeEvent,
)

T = TypeVar("T")
EventHandler = Callable[[T], Awaitable[None]]


class SignaldClient(SignaldRPCClient):
    _event_handlers: Dict[Type[T], List[EventHandler]]
    _subscriptions: Set[str]

    def __init__(
        self,
        socket_path: str = "/var/run/signald/signald.sock",
        log: Optional[TraceLogger] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        super().__init__(socket_path, log, loop)
        self._event_handlers = {}
        self._subscriptions = set()
        self.add_rpc_handler("message", self._parse_message)
        self.add_rpc_handler(
            "websocket_connection_state_change", self._websocket_connection_state_change
        )
        self.add_rpc_handler("version", self._log_version)
        self.add_rpc_handler(CONNECT_EVENT, self._resubscribe)
        self.add_rpc_handler(DISCONNECT_EVENT, self._on_disconnect)

    def add_event_handler(self, event_class: Type[T], handler: EventHandler) -> None:
        self._event_handlers.setdefault(event_class, []).append(handler)

    def remove_event_handler(self, event_class: Type[T], handler: EventHandler) -> None:
        self._event_handlers.setdefault(event_class, []).remove(handler)

    async def _run_event_handler(self, event: T) -> None:
        try:
            handlers = self._event_handlers[type(event)]
        except KeyError:
            self.log.warning(f"No handlers for {type(event)}")
        else:
            for handler in handlers:
                try:
                    await handler(event)
                except Exception:
                    self.log.exception("Exception in event handler")

    async def _parse_message(self, data: Dict[str, Any]) -> None:
        event_type = data["type"]
        event_data = data["data"]
        event_class = {
            "message": Message,
        }[event_type]
        event = event_class.deserialize(event_data)
        await self._run_event_handler(event)

    async def _log_version(self, data: Dict[str, Any]) -> None:
        name = data["data"]["name"]
        version = data["data"]["version"]
        self.log.info(f"Connected to {name} v{version}")

    async def _websocket_connection_state_change(self, change_event: Dict[str, Any]) -> None:
        evt = WebsocketConnectionStateChangeEvent.deserialize(change_event["data"])
        await self._run_event_handler(evt)

    async def subscribe(self, username: str) -> bool:
        try:
            await self.request("subscribe", "subscribed", username=username)
            self._subscriptions.add(username)
            return True
        except UnexpectedError as e:
            self.log.debug("Failed to subscribe to %s: %s", username, e)
            evt = WebsocketConnectionStateChangeEvent(
                state=(
                    WebsocketConnectionState.AUTHENTICATION_FAILED
                    if str(e) == "[401] Authorization failed!"
                    else WebsocketConnectionState.DISCONNECTED
                ),
                account=username,
            )
            await self._run_event_handler(evt)
            return False

    async def unsubscribe(self, username: str) -> bool:
        try:
            await self.request("unsubscribe", "unsubscribed", username=username)
            self._subscriptions.remove(username)
            return True
        except UnexpectedError as e:
            self.log.debug("Failed to unsubscribe from %s: %s", username, e)
            return False

    async def _resubscribe(self, unused_data: Dict[str, Any]) -> None:
        if self._subscriptions:
            self.log.debug("Resubscribing to users")
            for username in list(self._subscriptions):
                await self.subscribe(username)

    async def _on_disconnect(self, *_) -> None:
        if self._subscriptions:
            self.log.debug("Notifying of disconnection from users")
            for username in self._subscriptions:
                evt = WebsocketConnectionStateChangeEvent(
                    state=WebsocketConnectionState.SOCKET_DISCONNECTED,
                    account=username,
                    exception="Disconnected from signald",
                )
                await self._run_event_handler(evt)

    async def register(
        self, phone: str, voice: bool = False, captcha: Optional[str] = None
    ) -> str:
        resp = await self.request_v1("register", account=phone, voice=voice, captcha=captcha)
        return resp["account_id"]

    async def verify(self, username: str, code: str) -> Account:
        resp = await self.request_v1("verify", account=username, code=code)
        return Account.deserialize(resp)

    async def start_link(self) -> LinkSession:
        return LinkSession.deserialize(await self.request_v1("generate_linking_uri"))

    async def finish_link(
        self, session_id: str, device_name: str = "mausignald", overwrite: bool = False
    ) -> Account:
        resp = await self.request_v1(
            "finish_link", device_name=device_name, session_id=session_id, overwrite=overwrite
        )
        return Account.deserialize(resp)

    @staticmethod
    def _recipient_to_args(
        recipient: Union[Address, GroupID], simple_name: bool = False
    ) -> Dict[str, Any]:
        if isinstance(recipient, Address):
            recipient = recipient.serialize()
            field_name = "address" if simple_name else "recipientAddress"
        else:
            field_name = "group" if simple_name else "recipientGroupId"
        return {field_name: recipient}

    async def react(
        self, username: str, recipient: Union[Address, GroupID], reaction: Reaction
    ) -> None:
        await self.request_v1(
            "react",
            username=username,
            reaction=reaction.serialize(),
            **self._recipient_to_args(recipient),
        )

    async def remote_delete(
        self, username: str, recipient: Union[Address, GroupID], timestamp: int
    ) -> None:
        await self.request_v1(
            "remote_delete",
            account=username,
            timestamp=timestamp,
            **self._recipient_to_args(recipient, simple_name=True),
        )

    async def send(
        self,
        username: str,
        recipient: Union[Address, GroupID],
        body: str,
        quote: Optional[Quote] = None,
        attachments: Optional[List[Attachment]] = None,
        mentions: Optional[List[Mention]] = None,
        timestamp: Optional[int] = None,
    ) -> None:
        serialized_quote = quote.serialize() if quote else None
        serialized_attachments = [attachment.serialize() for attachment in (attachments or [])]
        serialized_mentions = [mention.serialize() for mention in (mentions or [])]
        resp = await self.request_v1(
            "send",
            username=username,
            messageBody=body,
            attachments=serialized_attachments,
            quote=serialized_quote,
            mentions=serialized_mentions,
            timestamp=timestamp,
            **self._recipient_to_args(recipient),
        )

        # We handle unregisteredFailure a little differently than other errors. If there are no
        # successful sends, then we show an error with the unregisteredFailure details, otherwise
        # we ignore it.
        errors = []
        unregistered_failures = []
        successful_send_count = 0
        results = resp.get("results", [])
        for result in results:
            address = result.get("addres", {})
            number = address.get("number") or address.get("uuid")
            proof_required_failure = result.get("proof_required_failure")
            if result.get("networkFailure", False):
                errors.append(f"Network failure occurred while sending message to {number}.")
            elif result.get("unregisteredFailure", False):
                unregistered_failures.append(
                    f"Unregistered failure occurred while sending message to {number}."
                )
            elif result.get("identityFailure", ""):
                errors.append(
                    f"Identity failure occurred while sending message to {number}. New identity: "
                    f"{result['identityFailure']}"
                )
            elif proof_required_failure:
                options = proof_required_failure.get("options")
                self.log.warning(
                    f"Proof Required Failure {options}. "
                    f"Retry after: {proof_required_failure.get('retry_after')}. "
                    f"Token: {proof_required_failure.get('token')}. "
                    f"Message: {proof_required_failure.get('message')}. "
                )
                errors.append(
                    f"Proof required failure occurred while sending message to {number}. Message: "
                    f"{proof_required_failure.get('message')}"
                )
                if "RECAPTCHA" in options:
                    errors.append("RECAPTCHA required.")
                elif "PUSH_CHALLENGE" in options:
                    # Just submit the challenge automatically.
                    await self.request_v1("submit_challenge")
            else:
                successful_send_count += 1
        self.log.info(
            f"Successfully sent message to {successful_send_count}/{len(results)} users in "
            f"{recipient} with {len(unregistered_failures)} unregistered failures"
        )
        if len(unregistered_failures) == len(results):
            errors.extend(unregistered_failures)
        if errors:
            raise Exception("\n".join(errors))

    async def send_receipt(
        self,
        username: str,
        sender: Address,
        timestamps: List[int],
        when: Optional[int] = None,
        read: bool = False,
    ) -> None:
        if not read:
            # TODO implement
            return
        await self.request_v1(
            "mark_read", account=username, timestamps=timestamps, when=when, to=sender.serialize()
        )

    async def list_accounts(self) -> List[Account]:
        resp = await self.request_v1("list_accounts")
        return [Account.deserialize(acc) for acc in resp.get("accounts", [])]

    async def delete_account(self, username: str, server: bool = False) -> None:
        await self.request_v1("delete_account", account=username, server=server)

    async def get_linked_devices(self, username: str) -> List[DeviceInfo]:
        resp = await self.request_v1("get_linked_devices", account=username)
        return [DeviceInfo.deserialize(dev) for dev in resp.get("devices", [])]

    async def remove_linked_device(self, username: str, device_id: int) -> None:
        await self.request_v1("remove_linked_device", account=username, deviceId=device_id)

    async def list_contacts(self, username: str) -> List[Profile]:
        resp = await self.request_v1("list_contacts", account=username)
        return [Profile.deserialize(contact) for contact in resp["profiles"]]

    async def list_groups(self, username: str) -> List[Union[Group, GroupV2]]:
        resp = await self.request_v1("list_groups", account=username)
        legacy = [Group.deserialize(group) for group in resp.get("legacyGroups", [])]
        v2 = [GroupV2.deserialize(group) for group in resp.get("groups", [])]
        return legacy + v2

    async def update_group(
        self,
        username: str,
        group_id: GroupID,
        title: Optional[str] = None,
        avatar_path: Optional[str] = None,
        add_members: Optional[List[Address]] = None,
        remove_members: Optional[List[Address]] = None,
    ) -> Union[Group, GroupV2, None]:
        update_params = {
            key: value
            for key, value in {
                "groupID": group_id,
                "avatar": avatar_path,
                "title": title,
                "addMembers": [addr.serialize() for addr in add_members] if add_members else None,
                "removeMembers": (
                    [addr.serialize() for addr in remove_members] if remove_members else None
                ),
            }.items()
            if value is not None
        }
        resp = await self.request_v1("update_group", account=username, **update_params)
        if "v1" in resp:
            return Group.deserialize(resp["v1"])
        elif "v2" in resp:
            return GroupV2.deserialize(resp["v2"])
        else:
            return None

    async def accept_invitation(self, username: str, group_id: GroupID) -> GroupV2:
        resp = await self.request_v1("accept_invitation", account=username, groupID=group_id)
        return GroupV2.deserialize(resp)

    async def get_group(
        self, username: str, group_id: GroupID, revision: int = -1
    ) -> Optional[GroupV2]:
        resp = await self.request_v1(
            "get_group", account=username, groupID=group_id, revision=revision
        )
        if "id" not in resp:
            return None
        return GroupV2.deserialize(resp)

    async def get_profile(
        self, username: str, address: Address, use_cache: bool = False
    ) -> Optional[Profile]:
        try:
            # async is a reserved keyword, so can't pass it as a normal parameter
            kwargs = {"async": use_cache}
            resp = await self.request_v1(
                "get_profile", account=username, address=address.serialize(), **kwargs
            )
        except UnexpectedResponse as e:
            if e.resp_type == "profile_not_available":
                return None
            raise
        return Profile.deserialize(resp)

    async def get_identities(self, username: str, address: Address) -> GetIdentitiesResponse:
        resp = await self.request_v1(
            "get_identities", account=username, address=address.serialize()
        )
        return GetIdentitiesResponse.deserialize(resp)

    async def set_profile(
        self, username: str, name: Optional[str] = None, avatar_path: Optional[str] = None
    ) -> None:
        args = {}
        if name is not None:
            args["name"] = name
        if avatar_path is not None:
            args["avatarFile"] = avatar_path
        await self.request_v1("set_profile", account=username, **args)

    async def trust(
        self,
        username: str,
        recipient: Address,
        trust_level: str,
        safety_number: Optional[str] = None,
        qr_code_data: Optional[str] = None,
    ) -> None:
        args = {}
        if safety_number:
            if qr_code_data:
                raise ValueError("only one of safety_number and qr_code_data must be set")
            args["safety_number"] = safety_number
        elif qr_code_data:
            args["qr_code_data"] = qr_code_data
        else:
            raise ValueError("safety_number or qr_code_data is required")
        await self.request_v1(
            "trust",
            account=username,
            **args,
            trust_level=trust_level,
            address=recipient.serialize(),
        )
