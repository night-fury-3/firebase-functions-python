# Copyright 2022 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Cloud functions to handle events from Google Cloud Pub/Sub.
"""
# pylint: disable=protected-access
import dataclasses as _dataclasses
import datetime as _dt
import functools as _functools
import typing as _typing
import json as _json
import base64 as _base64
import cloudevents.http as _ce

import firebase_functions.options as _options
import firebase_functions.private.util as _util
from firebase_functions.core import CloudEvent, T


@_dataclasses.dataclass(frozen=True)
class Message(_typing.Generic[T]):
    """
    Interface representing a Google Cloud Pub/Sub message.
    """

    message_id: str
    """
    Autogenerated ID that uniquely identifies this message.
    """

    publish_time: str
    """
    Time the message was published.
    """

    attributes: dict[str, str]
    """
    User-defined attributes published with the message, if any.
    """

    data: str
    """
    The data payload of this message object as a base64-encoded string.
    """

    ordering_key: str
    """
    User-defined key used to ensure ordering amongst messages with the same key.
    """

    @property
    def json(self) -> _typing.Optional[T]:
        try:
            if self.data is not None:
                return _json.loads(_base64.b64decode(self.data).decode("utf-8"))
            else:
                return None
        except Exception as error:
            raise ValueError(
                f"Unable to parse Pub/Sub message data as JSON: {error}"
            ) from error


@_dataclasses.dataclass(frozen=True)
class MessagePublishedData(_typing.Generic[T]):
    """
    The interface published in a Pub/Sub publish subscription.

    'T' Type representing `Message.data`'s JSON format.
    """
    message: Message[T]
    """
    Google Cloud Pub/Sub message.
    """

    subscription: str
    """
    A subscription resource.
    """


_E1 = CloudEvent[MessagePublishedData[T]]
_C1 = _typing.Callable[[_E1], None]


def _message_handler(
    func: _C1,
    raw: _ce.CloudEvent,
) -> None:
    event_attributes = raw._get_attributes()
    event_data: _typing.Any = raw.get_data()
    event_dict = {"data": event_data, **event_attributes}
    data = event_dict["data"]
    message_dict = data["message"]

    time = _dt.datetime.strptime(
        event_dict["time"],
        "%Y-%m-%dT%H:%M:%S.%f%z",
    )

    publish_time = _dt.datetime.strptime(
        message_dict["publish_time"],
        "%Y-%m-%dT%H:%M:%S.%f%z",
    )

    # Convert the UTC string into a datetime object
    event_dict["time"] = time
    message_dict["publish_time"] = publish_time

    # Pop unnecessary keys from the message data
    # (we get these keys from the snake case alternatives that are provided)
    message_dict.pop("messageId", None)
    message_dict.pop("publishTime", None)

    # `orderingKey` doesn't come with a snake case alternative,
    # there is no `ordering_key` in the raw request.
    ordering_key = message_dict.pop("orderingKey", None)

    # Include empty attributes property if missing
    message_dict["attributes"] = message_dict.get("attributes", {})

    message: MessagePublishedData = MessagePublishedData(
        message=Message(
            **message_dict,
            ordering_key=ordering_key,
        ),
        subscription=data["subscription"],
    )

    event_dict["data"] = message

    event: CloudEvent[MessagePublishedData] = CloudEvent(
        data=event_dict["data"],
        id=event_dict["id"],
        source=event_dict["source"],
        specversion=event_dict["specversion"],
        subject=event_dict["subject"] if "subject" in event_dict else None,
        time=event_dict["time"],
        type=event_dict["type"],
    )

    func(event)


@_util.copy_func_kwargs(_options.PubSubOptions)
def on_message_published(**kwargs) -> _typing.Callable[[_C1], _C1]:
    """
    Event handler which triggers on a message being published to a Pub/Sub topic.

    Example:

    .. code-block:: python

      @on_message_published(topic="hello-world")
      def example(event: CloudEvent[MessagePublishedData[object]]) -> None:
          pass

    """
    options = _options.PubSubOptions(**kwargs)

    def on_message_published_inner_decorator(func: _C1):

        @_functools.wraps(func)
        def on_message_published_wrapped(raw: _ce.CloudEvent):
            return _message_handler(func, raw)

        _util.set_func_endpoint_attr(
            on_message_published_wrapped,
            options._endpoint(func_name=func.__name__),
        )
        return on_message_published_wrapped

    return on_message_published_inner_decorator
