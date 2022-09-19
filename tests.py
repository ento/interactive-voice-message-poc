from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import create_app


@pytest.fixture
def app():
    return create_app(
        config_path=None,
        config_dict={
            "IVM": {
                "twilio_account_sid": "a",
                "twilio_auth_token": "b",
                "from_number": "from",
                "to_number": "to",
                "use_ngrok": False,
                "variables": {
                    "from_name": "From",
                    "subject": "Apple",
                    "main_message": "Apples are red",
                    "email": "test@example.com",
                }
            }
        },
    )


@pytest.fixture
def client(app):
    with app.test_client() as test_client, app.app_context():
        yield test_client


def test_index_page(client):
    res = client.get("/")
    assert res.status_code == 200


@pytest.mark.golden_test("goldens/start_call.yaml")
@patch("twilio.rest.Client.calls")
def test_start_call(mock_twilio_calls, golden, client):
    mock_call = MagicMock(sid="140233184318736")
    mock_twilio_calls.create.return_value = mock_call

    res = client.post("/calls")

    assert res.status_code == 200
    assert res.text == golden.out["output"]


@pytest.mark.golden_test("goldens/menu_callback_*.yaml")
def test_menu_callback_when_pressed_1(golden, client):
    res = client.post("/menu-callback", data={"Digits": golden["digits"]})
    assert res.text == golden.out["output"]


@pytest.mark.golden_test("goldens/voice_reply_callback.yaml")
def test_voice_reply_callback(golden, client):
    res = client.post("/voice-reply-callback")
    assert res.status_code == 200
    assert res.text == golden.out["output"]


def test_trascribe_callback(client):
    res = client.post("/transcribe-callback")
    assert res.status_code == 200
    assert res.text == ""
