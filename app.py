import argparse
import enum
import sys
from functools import partial, reduce
from string import Template
from typing import Any, Callable, Dict, List, Optional, TypeAlias, Union

import tomlkit
from flask import Blueprint, Flask, Response, current_app, request, url_for
from pydantic import BaseModel
from pyngrok import ngrok
from twilio.rest import Client
from twilio.twiml import TwiML
from twilio.twiml.voice_response import Say, VoiceResponse

# ------------------------------------------------------
# Types
# ------------------------------------------------------


class IVMConfig(BaseModel):
    twilio_account_sid: str
    twilio_auth_token: str
    use_ngrok: bool
    ngrok_auth_token: Optional[str]
    to_number: str
    from_number: str
    machine_voice: str = "Polly.Matthew-Neural"
    human_voice: str = "Polly.Salli-Neural"
    variables: Dict[str, str] = {}


class CallbackSideEffect(enum.Enum):
    """
    Common actions to take after handling user input.
    """

    NOOP = enum.auto()
    RETURN_TO_MENU = enum.auto()
    HANGUP = enum.auto()


CallbackAction: TypeAlias = Callable[[VoiceResponse], CallbackSideEffect]


class MenuItem(BaseModel):
    """
    Represents a choice in the interactive menu.
    """

    # What to say after "Press [digit]", e.g. "to hear the message again"
    prompt: str
    # Action to perform when this menu item is selected by the user
    callback_action: CallbackAction


# Union of types that can be handled by our utility functions
# when constructing TwiML
LazyTwiMLBuilder = Callable[[Say], None]
TwiMLElement: TypeAlias = Union[str, LazyTwiMLBuilder, TwiML]


# ------------------------------------------------------
# TwiML helpers
# ------------------------------------------------------


def wrap_in_list(obj: Any) -> List[Any]:
    """
    If 'obj' is a string or something non-iterable, wrap it in a list.
    """
    if isinstance(obj, str):
        return [obj]
    try:
        iter(obj)
        return obj
    except TypeError:
        return [obj]


def concat_consecutive_strings(elems: List[TwiMLElement]) -> List[TwiMLElement]:
    """
    If you call TwiML.append() with a string argument multiple times,
    only the last string gets actually appended. Concatenate consecutive
    strings in the given list to avoid that.
    """

    def reducer(acc: List[TwiMLElement], elem: TwiMLElement) -> List[TwiMLElement]:
        if acc and isinstance(acc[-1], str) and isinstance(elem, str):
            acc[-1] += elem
        else:
            acc.append(elem)
        return acc

    return reduce(reducer, elems, [])


def say(children: Union[TwiMLElement, List[TwiMLElement]], **kwargs) -> Say:
    """
    Construct a Say element from the given children.
    """
    twiml = Say(**kwargs)
    for elem in concat_consecutive_strings(wrap_in_list(children)):
        if isinstance(elem, str):
            rendered = render_template(elem)
            twiml.append(rendered)
        elif isinstance(elem, TwiML):
            twiml.append(elem)
        else:
            elem(twiml)
    return twiml


def say_as_machine(messages: Union[TwiMLElement, List[TwiMLElement]], **kwargs) -> Say:
    """
    Construct a Say element with voice set to the `machine_voice` config.
    """
    kwargs["voice"] = current_app.config["IVM"].machine_voice
    return say(messages, **kwargs)


def say_as_human(messages: Union[TwiMLElement, List[TwiMLElement]], **kwargs) -> Say:
    """
    Construct a Say element with voice set to the `human_voice` config.
    """
    kwargs["voice"] = current_app.config["IVM"].human_voice
    return say(messages, **kwargs)


def render_twiml(resp: VoiceResponse) -> Response:
    """
    Given a VoiceResponse, render it as Flask response.
    """
    flask_response = Response(str(resp))
    flask_response.headers["Content-Type"] = "text/xml"
    return flask_response


def render_template(template: str) -> str:
    """
    Render the given string a string.Template. You can reference values
    from the `variables` config by writing "$var_name".
    """
    config = current_app.config["IVM"]
    return Template(template).substitute(**config.variables)


def ssml(say_attr_name: str, *args, **kwargs) -> LazyTwiMLBuilder:
    """
    Return a function that, when given a Say element, invokes its
    attribute named `say_attr_name` with the given args and kwargs.
    """

    def call_attr(say: Say) -> None:
        getattr(say, say_attr_name)(*args, **kwargs)

    return call_attr


# ------------------------------------------------------
# Things that the bot can say and do
# ------------------------------------------------------


class DynamicMessages:
    """
    Messages that reference config variables.
    """

    @staticmethod
    def intro() -> Say:
        return say_as_machine(
            "Hello, this is a voice message from $from_name about $subject."
        )

    @staticmethod
    def parting() -> Say:
        return say_as_machine(
            "Thank you. Your reply will be delivered to $from_name. I hope you have a nice day."
        )

    @staticmethod
    def main() -> Say:
        return say_as_human("$main_message")

    @staticmethod
    def email_address() -> Say:
        email = render_template("$email")
        return say_as_human(
            [
                "My email address is,",
                ssml("say_as", email, interpret_as="spell-out"),
            ]
        )


class CompositeActions:
    """
    Reusable actions to take (not necessarily in response to user input).
    """

    @staticmethod
    def say_menu(
        menu: Dict[str, MenuItem], response: VoiceResponse, external: bool = False
    ) -> None:
        with current_app.test_request_context(
            base_url=current_app.config["BASE_URL"]
        ), response.gather(
            num_digits=1,
            action=url_for("voice_message.handle_menu_callback", _external=external),
            method="POST",
            timeout=120,
        ) as g:
            options = []
            for index, (digits, menu_item) in enumerate(menu.items()):
                prefix = "Please press" if index == 0 else "Press"
                options.append(f"{prefix} {digits} {menu_item.prompt}. ")
            options.extend(
                [
                    "Press any other key to repeat the options. ",
                    "Or, please feel free to hang up now. ",
                    "I will wait for 2 minutes before ending the call.",
                ]
            )
            g.append(say_as_machine(options, loop=1))
        response.append(say_as_machine("Okay, thank you very much!"))


class CallbackActions:
    """
    A collection of functions that take a VoiceResponse, append
    elements, and return what further action to take.
    """

    @staticmethod
    def say_message(
        message: Callable[[], Say], response: VoiceResponse
    ) -> CallbackSideEffect:
        response.append(message())
        response.pause(1)
        return CallbackSideEffect.RETURN_TO_MENU

    @staticmethod
    def say_message_and_hangup(
        message: Callable[[], Say], response: VoiceResponse
    ) -> CallbackSideEffect:
        response.append(message())
        response.pause(1)
        return CallbackSideEffect.HANGUP

    @staticmethod
    def prompt_voice_reply(response: VoiceResponse) -> CallbackSideEffect:
        response.append(
            say_as_machine(
                "Please leave a reply after you hear a beep. Press the pound sign to finish recording."
            )
        )
        response.record(
            action=url_for(".handle_voice_reply_callback"),
            play_beep=True,
            method="POST",
            max_length=120,
            transcribe_callback=url_for(".handle_transcribe_callback"),
        )
        return CallbackSideEffect.NOOP

    @staticmethod
    def return_to_menu(response: VoiceResponse):
        return CallbackSideEffect.RETURN_TO_MENU


def run_callback_action(action: CallbackAction, response: VoiceResponse) -> None:
    """
    Invoke the given action and perform any side-effect specified by the action.
    """
    side_effect = action(response)
    match side_effect:
        case CallbackSideEffect.RETURN_TO_MENU:
            CompositeActions.say_menu(interactive_menu, response, external=False)
        case CallbackSideEffect.HANGUP:
            response.hangup()


# An interactive menu: a mapping from numpad keys to MenuItems.
# This is pretty much coupled with other parts of the code base.
# We could further generalize the idea of a menu constructor and
# make it possible to define interactive menus declaratively in
# config files or database models.
interactive_menu = {
    "1": MenuItem(
        prompt="to play the message again",
        callback_action=partial(CallbackActions.say_message, DynamicMessages.main),
    ),
    "2": MenuItem(
        prompt="for the sender's email address",
        callback_action=partial(CallbackActions.say_message, DynamicMessages.email_address),
    ),
    "3": MenuItem(
        prompt="to record a voice message to be sent back",
        callback_action=CallbackActions.prompt_voice_reply,
    ),
}

# ------------------------------------------------------
# HTTP routes
# ------------------------------------------------------

# We need HTTP endpoints that can handle numpad inputs forwarded
# by Twilio.
vm = Blueprint("voice_message", __name__, url_prefix="/")


@vm.route("/", methods=("GET",))
def handle_index_page():
    """
    Show a simple form with a button to initiate a call.
    """
    return f"""
<form method=POST action={url_for(".start_call")}>
  <input type="submit" value="Send interactive voice message">
</form>
"""


@vm.route("/calls", methods=("POST",))
def start_call():
    """
    Send an interactive voice message. Recipient and message content are
    defined in the config.
    """
    client = create_twilio_client()

    response = VoiceResponse()
    response.append(DynamicMessages.intro())
    response.pause(1)
    response.append(DynamicMessages.main())
    response.append(DynamicMessages.email_address())
    response.pause(1)

    CompositeActions.say_menu(interactive_menu, response, external=True)

    call = client.calls.create(
        twiml=response,
        to=current_app.config["IVM"].to_number,
        from_=current_app.config["IVM"].from_number,
    )

    return f"Initiated call with SID {call.sid} with the following VoiceResponse:\n{str(response)}"


@vm.route("/menu-callback", methods=("POST",))
def handle_menu_callback():
    """
    Invoked when the call recipient presses a numpad key in response to
    the interactive menu.
    """
    response = VoiceResponse()
    digits = request.form["Digits"]
    menu_item = interactive_menu.get(digits)
    run_callback_action(
        menu_item.callback_action if menu_item else lambda _res: CallbackSideEffect.RETURN_TO_MENU,
        response,
    )
    return render_twiml(response)


@vm.route("/voice-reply-callback", methods=("POST",))
def handle_voice_reply_callback():
    """
    Invoked when the call recipient chose to reply with a voice recording
    and finished recording their reply.
    """
    response = VoiceResponse()
    action = partial(CallbackActions.say_message_and_hangup, DynamicMessages.parting)
    run_callback_action(action, response)
    return render_twiml(response)


@vm.route("/transcribe-callback", methods=("POST",))
def handle_transcribe_callback():
    """
    Invoked when Twilio finished transcribing the recipient's voice recording.
    """
    return ""  # Do nothing


# ------------------------------------------------------
# Initializers
# ------------------------------------------------------


def create_twilio_client():
    account_sid = current_app.config["IVM"].twilio_account_sid
    auth_token = current_app.config["IVM"].twilio_auth_token
    return Client(account_sid, auth_token)


def create_app(config_path: Optional[str] = None, config_dict: Optional[dict] = None):
    app = Flask(__name__)

    app.config.from_mapping(
        BASE_URL="http://localhost:5000",
    )
    if config_path:
        app.config.from_file(config_path, load=tomlkit.loads)
    if config_dict:
        app.config.from_mapping(config_dict)
    # Env vars take precedence
    app.config.from_prefixed_env()
    # Let Pydantic re-parse the config
    app.config["IVM"] = IVMConfig(**app.config.get("IVM", {}))

    if app.config["IVM"].use_ngrok:
        # Get the dev server port (defaults to 5000 for Flask, can be overridden with `--port`
        # when starting the server
        port = sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else 5000

        # Set auth token if specified
        if app.config["IVM"].ngrok_auth_token:
            ngrok.set_auth_token(app.config["IVM"].ngrok_auth_token)

        # Open an ngrok tunnel to the dev server
        public_url = ngrok.connect(port, bind_tls=True).public_url
        print(f" * ngrok tunnel {public_url} -> http://127.0.0.1:{port}")

        app.config["BASE_URL"] = public_url
        app.config["PREFERRED_URL_SCHEME"] = "https"

    app.register_blueprint(vm)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args()

    app = create_app(config_path=args.config)
    app.run(use_reloader=False)
