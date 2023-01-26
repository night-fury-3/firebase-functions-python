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
Module for internal utilities.
"""

import os as _os
import json as _json
import typing as _typing
import dataclasses as _dataclasses
import enum as _enum
from flask import Request as _Request
from functions_framework import logging as _logging
from firebase_admin import auth as _auth
from firebase_admin import app_check as _app_check

P = _typing.ParamSpec("P")
R = _typing.TypeVar("R")


class Sentinel:
    """Internal class for USE_DEFAULT."""

    def __init__(self, description):
        self.description = description


def copy_func_kwargs(
    func_with_kwargs: _typing.Callable[P, _typing.Any],  # pylint: disable=unused-argument
) -> _typing.Callable[[_typing.Callable[..., R]], _typing.Callable[P, R]]:

    def return_func(func: _typing.Callable[..., R]) -> _typing.Callable[P, R]:
        return _typing.cast(_typing.Callable[P, R], func)

    return return_func


def set_func_endpoint_attr(
        func: _typing.Callable[P, _typing.Any],
        endpoint: _typing.Any) -> _typing.Callable[P, _typing.Any]:
    setattr(func, "__firebase_endpoint__", endpoint)
    return func


def prune_nones(obj: dict) -> dict:
    for key in obj:
        if obj[key] is None:
            del obj[key]
        elif isinstance(obj[key], dict):
            prune_nones(obj[key])
    return obj


def valid_on_call_request(request: _Request) -> bool:
    """Validate request"""
    if (_on_call_valid_method(request) and
            _on_call_valid_content_type(request) and
            _on_call_valid_body(request)):
        return True
    return False


def _on_call_valid_body(request: _Request) -> bool:
    """The body must not be empty."""
    if request.json is None:
        _logging.warning("Request is missing body.")
        return False

    # The body must have data.
    if request.json is None or "data" not in request.json:
        _logging.warning("Request body is missing data.", request.json)
        return False

    extra_keys = {
        key: request.json[key] for key in request.json.keys() if key != "data"
    }
    if len(extra_keys) != 0:
        _logging.warning(
            "Request body has extra fields: ",
            "".join(f"{key}: {value}," for (key, value) in extra_keys.items()),
        )
        return False
    return True


def _on_call_valid_method(request: _Request) -> bool:
    """Make sure it's a POST."""
    if request.method != "POST":
        _logging.warning("Request has invalid method.", request.method)
        return False
    return True


def _on_call_valid_content_type(request: _Request) -> bool:
    """Validate content"""
    content_type: _typing.Optional[str] = request.headers.get("Content-Type")

    if content_type is None:
        _logging.warning("Request is missing Content-Type.", content_type)
        return False

    # If it has a charset, just ignore it for now.
    try:
        semi_colon = content_type.index(";")
        if semi_colon >= 0:
            content_type = content_type[0:semi_colon].strip()
    except ValueError:
        pass

    # Check that the Content-Type is JSON.
    if content_type.lower() != "application/json":
        _logging.warning("Request has incorrect Content-Type.", content_type)
        return False

    return True


class OnCallTokenState(_enum.Enum):
    """
    The status of a token.
    """

    MISSING = "MISSING"
    """
    There is no token, e.g. unauthenticated requests.
    """

    VALID = "VALID"
    """
    The token is valid.
    """

    INVALID = "INVALID"
    """
    The token is invalid.
    """


@_dataclasses.dataclass()
class _OnCallTokenVerification:
    """
    Internal class used to hold verification information of tokens used in
    on_call https requests (auth + app check tokens).
    """

    app: OnCallTokenState = OnCallTokenState.INVALID
    app_token: _typing.Optional[dict] = None
    auth: OnCallTokenState = OnCallTokenState.INVALID
    auth_token: _typing.Optional[dict] = None

    def as_dict(self) -> dict:
        """Set dictionary"""
        return {
            "app": self.app.value if self.app is not None else None,
            "auth": self.auth.value if self.auth is not None else None,
        }


def _on_call_check_auth_token(
    request: _Request
) -> None | _typing.Literal[OnCallTokenState.INVALID] | dict[str, _typing.Any]:
    """Validates the auth token in a callable request."""
    authorization = request.headers.get("Authorization")
    if authorization is None:
        return None
    if not authorization.startswith("Bearer "):
        _logging.error("Error validating token: Not a bearer token")
        return OnCallTokenState.INVALID
    try:
        id_token = authorization.replace("Bearer ", "")
        auth_token = _auth.verify_id_token(id_token)
        return auth_token
    # pylint: disable=broad-except
    except Exception as err:
        _logging.error(f"Error validating token: {err}")
        return OnCallTokenState.INVALID
    return OnCallTokenState.INVALID


def _on_call_check_app_token(
    request: _Request
) -> None | _typing.Literal[OnCallTokenState.INVALID] | dict[str, _typing.Any]:
    """Validates the app token in a callable request."""
    app_check = request.headers.get("X-Firebase-AppCheck")
    if app_check is None:
        return None
    try:
        app_token = _app_check.verify_token(app_check)
        return app_token
    # pylint: disable=broad-except
    except Exception as err:
        _logging.error(f"Error validating token: {err}")
        return OnCallTokenState.INVALID


def on_call_check_tokens(request: _Request,) -> _OnCallTokenVerification:
    """Check tokens"""
    verifications = _OnCallTokenVerification()

    auth_token = _on_call_check_auth_token(request)
    if auth_token is None:
        verifications.auth = OnCallTokenState.MISSING
    elif isinstance(auth_token, dict):
        verifications.auth = OnCallTokenState.VALID
        verifications.auth_token = auth_token

    app_token = _on_call_check_app_token(request)
    if app_token is None:
        verifications.app = OnCallTokenState.MISSING
    elif isinstance(app_token, dict):
        verifications.app = OnCallTokenState.VALID
        verifications.app_token = app_token

    log_payload = {
        **verifications.as_dict(),
        "logging.googleapis.com/labels": {
            "firebase-log-type": "callable-request-verification",
        },
    }

    errs = []
    if verifications.app == OnCallTokenState.INVALID:
        errs.append(("AppCheck token was rejected.", log_payload))

    if verifications.auth == OnCallTokenState.INVALID:
        errs.append(("Auth token was rejected.", log_payload))

    if len(errs) == 0:
        _logging.info("Callable request verification passed", log_payload)
    else:
        _logging.warning(f"Callable request verification failed: ${errs}",
                         log_payload)

    return verifications


@_dataclasses.dataclass(frozen=True)
class FirebaseConfig():
    """
    A collection of configuration options needed to 
    initialize a firebase App.
    """

    storage_bucket: _typing.Optional[str]
    """
    The name of the Google Cloud Storage bucket used for storing application data.
    This is the bucket name without any prefixes or additions (without "gs://").
    """

    # TODO more to be added later when they are required


def firebase_config() -> None | FirebaseConfig:
    config_file = _os.getenv("FIREBASE_CONFIG")
    if not config_file:
        return None
    if config_file.startswith('{'):
        json_str = config_file
    else:
        # Firebase Tools will always use a JSON blob in prod, but docs
        # explicitly state that the user can set the env to a file:
        # https://firebase.google.com/docs/admin/setup#initialize-without-parameters
        try:
            with open(config_file, 'r') as json_file:
                json_str = json_file.read()
        except Exception as err:
            raise ValueError('Unable to read file {}. {}'.format(
                config_file, err))
    try:
        json_data: dict = _json.loads(json_str)
    except Exception as err:
        raise ValueError(
            'FIREBASE_CONFIG JSON string "{0}" is not valid json. {1}'.format(
                json_str, err))
    return FirebaseConfig(storage_bucket=json_data.get('storageBucket'))
