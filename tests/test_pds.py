"""After sign-in, every signed-in call goes to the account's own server (the PDS its DID document names), as Bluesky's own apps
do — not to the sign-in service. Chat calls proxied through the sign-in service were refused with ``MethodNotImplemented``."""

import pytest
from conftest import DID, HANDLE, SERVICE, make_ctx, reply

from marvin_integration_bluesky import BlueskyProvider
from marvin_integration_bluesky.provider import pds_endpoint

P = BlueskyProvider()
PDS = "https://shiitake.us-east.host.bsky.network"
ALICE = "did:plc:alice"


def did_doc(endpoint=PDS, service_id="#atproto_pds"):
    return {"id": DID, "service": [{"id": service_id, "type": "AtprotoPersonalDataServer", "serviceEndpoint": endpoint}]}


def signs_in_with(http, doc):
    http.routes[f"{SERVICE}/xrpc/com.atproto.server.createSession"] = lambda c: reply(
        body={"accessJwt": "jwt-1", "refreshJwt": "r-1", "did": DID, "handle": HANDLE, **({"didDoc": doc} if doc else {})}
    )


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        (did_doc(), PDS),
        (did_doc(PDS + "/"), PDS),
        (did_doc(service_id=f"{DID}#atproto_pds"), PDS),  # a fully qualified id
        (did_doc("http://pds.example"), None),  # never plain http: the token would travel in the clear
        (did_doc("https://pds.example?x=1"), None),
        (did_doc(service_id="#bsky_chat"), None),
        ({"service": "nope"}, None),
        (None, None),
    ],
)
def test_the_pds_is_read_from_the_did_document_only_when_it_is_a_plain_https_address(doc, expected):
    assert pds_endpoint(doc) == expected


def test_signed_in_calls_and_chat_go_to_the_accounts_own_server(http):
    signs_in_with(http, did_doc())
    http.routes[f"{PDS}/xrpc/chat.bsky.convo.getConvoForMembers"] = lambda c: reply(body={"convo": {"id": "convo-1", "members": []}})
    http.routes[f"{PDS}/xrpc/chat.bsky.convo.sendMessage"] = lambda c: reply(body={"id": "msg-1", "rev": "r1", "text": "hi"})

    out = P.run_action("message", {"to": {"id": ALICE}, "text": "hi"}, make_ctx(http))

    assert out["sent"] is True
    urls = [c["url"] for c in http.calls]
    assert urls[0] == f"{SERVICE}/xrpc/com.atproto.server.createSession"  # sign-in stays on the sign-in service
    assert all(u.startswith(PDS) for u in urls[1:]) and len(urls) == 3
    assert http.calls[1]["headers"]["atproto-proxy"] == "did:web:api.bsky.chat#bsky_chat"


def test_without_a_did_document_everything_stays_on_the_sign_in_service(http):
    signs_in_with(http, None)
    http.routes[f"{SERVICE}/xrpc/chat.bsky.convo.getConvoForMembers"] = lambda c: reply(body={"convo": {"id": "convo-1", "members": []}})
    http.routes[f"{SERVICE}/xrpc/chat.bsky.convo.sendMessage"] = lambda c: reply(body={"id": "msg-1", "rev": "r1", "text": "hi"})

    P.run_action("message", {"to": {"id": ALICE}, "text": "hi"}, make_ctx(http))

    assert all(c["url"].startswith(SERVICE) for c in http.calls)
