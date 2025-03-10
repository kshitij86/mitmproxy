import io
import gzip
import json
import logging
import textwrap
from collections.abc import Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import Optional
from unittest import mock

import pytest
import tornado.testing
from tornado import httpclient
from tornado import websocket

from mitmproxy import certs, options, optmanager
from mitmproxy.http import Headers
from mitmproxy.test import tflow
from mitmproxy.tools.web import app
from mitmproxy.tools.web import master as webmaster


@pytest.fixture(scope="module")
def no_tornado_logging():
    logging.getLogger("tornado.access").disabled = True
    logging.getLogger("tornado.application").disabled = True
    logging.getLogger("tornado.general").disabled = True
    yield
    logging.getLogger("tornado.access").disabled = False
    logging.getLogger("tornado.application").disabled = False
    logging.getLogger("tornado.general").disabled = False


def get_json(resp: httpclient.HTTPResponse):
    return json.loads(resp.body.decode())


def test_generate_tflow_js(tdata):
    tf_http = tflow.tflow(resp=True, err=True, ws=True)
    tf_http.id = "d91165be-ca1f-4612-88a9-c0f8696f3e29"
    tf_http.client_conn.id = "4a18d1a0-50a1-48dd-9aa6-d45d74282939"
    tf_http.server_conn.id = "f087e7b2-6d0a-41a8-a8f0-e1a4761395f8"
    tf_http.server_conn.certificate_list = [
        certs.Cert.from_pem(
            Path(
                tdata.path("mitmproxy/net/data/verificationcerts/self-signed.pem")
            ).read_bytes()
        )
    ]
    tf_http.request.trailers = Headers(trailer="qvalue")
    tf_http.response.trailers = Headers(trailer="qvalue")
    tf_http.comment = "I'm a comment!"

    tf_tcp = tflow.ttcpflow(err=True)
    tf_tcp.id = "2ea7012b-21b5-4f8f-98cd-d49819954001"
    tf_tcp.client_conn.id = "8be32b99-a0b3-446e-93bc-b29982fe1322"
    tf_tcp.server_conn.id = "e33bb2cd-c07e-4214-9a8e-3a8f85f25200"

    tf_dns = tflow.tdnsflow(resp=True, err=True)
    tf_dns.id = "5434da94-1017-42fa-872d-a189508d48e4"
    tf_dns.client_conn.id = "0b4cc0a3-6acb-4880-81c0-1644084126fc"
    tf_dns.server_conn.id = "db5294af-c008-4098-a320-a94f901eaf2f"

    # language=TypeScript
    content = (
        "/** Auto-generated by test_app.py:test_generate_tflow_js */\n"
        "import {HTTPFlow, TCPFlow, DNSFlow} from '../../flow';\n"
        "export function THTTPFlow(): Required<HTTPFlow> {\n"
        "    return %s\n"
        "}\n"
        "export function TTCPFlow(): Required<TCPFlow> {\n"
        "    return %s\n"
        "}\n"
        "export function TDNSFlow(): Required<DNSFlow> {\n"
        "    return %s\n"
        "}\n"
        % (
            textwrap.indent(
                json.dumps(app.flow_to_json(tf_http), indent=4, sort_keys=True), "    "
            ),
            textwrap.indent(
                json.dumps(app.flow_to_json(tf_tcp), indent=4, sort_keys=True), "    "
            ),
            textwrap.indent(
                json.dumps(app.flow_to_json(tf_dns), indent=4, sort_keys=True), "    "
            ),
        )
    )
    content = content.replace(": null", ": undefined")

    (
        Path(__file__).parent / "../../../../web/src/js/__tests__/ducks/_tflow.ts"
    ).write_bytes(content.encode())


async def test_generate_options_js():
    o = options.Options()
    m = webmaster.WebMaster(o)
    opt: optmanager._Option

    def ts_type(t):
        if t == bool:
            return "boolean"
        if t == str:
            return "string"
        if t == int:
            return "number"
        if t == Sequence[str]:
            return "string[]"
        if t == Optional[str]:
            return "string | undefined"
        if t == Optional[int]:
            return "number | undefined"
        raise RuntimeError(t)

    with redirect_stdout(io.StringIO()) as s:

        print("/** Auto-generated by test_app.py:test_generate_options_js */")

        print("export interface OptionsState {")
        for _, opt in sorted(m.options.items()):
            print(f"    {opt.name}: {ts_type(opt.typespec)}")
        print("}")
        print("")
        print("export type Option = keyof OptionsState")
        print("")
        print("export const defaultState: OptionsState = {")
        for _, opt in sorted(m.options.items()):
            print(
                f"    {opt.name}: {json.dumps(opt.default)},".replace(
                    ": null", ": undefined"
                )
            )
        print("}")

    (
        Path(__file__).parent / "../../../../web/src/js/ducks/_options_gen.ts"
    ).write_bytes(s.getvalue().encode())


@pytest.mark.usefixtures("no_tornado_logging", "tdata")
class TestApp(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        async def make_master():
            o = options.Options(http2=False)
            return webmaster.WebMaster(o, with_termlog=False)

        m = self.io_loop.asyncio_loop.run_until_complete(make_master())
        f = tflow.tflow(resp=True)
        f.id = "42"
        f.request.content = b"foo\nbar"
        f2 = tflow.tflow(ws=True, resp=True)
        f2.request.content = None
        f2.response.content = None
        f2.id = "43"
        m.view.add([f, f2])
        m.view.add([tflow.tflow(err=True)])
        m.log.info("test log")
        self.master = m
        self.view = m.view
        self.events = m.events
        webapp = app.Application(m, None)
        webapp.settings["xsrf_cookies"] = False
        return webapp

    def fetch(self, *args, **kwargs) -> httpclient.HTTPResponse:
        # tornado disallows POST without content by default.
        return super().fetch(*args, **kwargs, allow_nonstandard_methods=True)

    def put_json(self, url, data: dict) -> httpclient.HTTPResponse:
        return self.fetch(
            url,
            method="PUT",
            body=json.dumps(data),
            headers={"Content-Type": "application/json"},
        )

    def test_index(self):
        assert self.fetch("/").code == 200

    def test_filter_help(self):
        assert self.fetch("/filter-help").code == 200

    def test_flows(self):
        resp = self.fetch("/flows")
        assert resp.code == 200
        assert get_json(resp)[0]["request"]["contentHash"]
        assert get_json(resp)[2]["error"]

    def test_flows_dump(self):
        resp = self.fetch("/flows/dump")
        assert b"address" in resp.body

    def test_clear(self):
        events = self.events.data.copy()
        flows = list(self.view)

        assert self.fetch("/clear", method="POST").code == 200

        assert not len(self.view)
        assert not len(self.events.data)

        # restore
        for f in flows:
            self.view.add([f])
        self.events.data = events

    def test_resume(self):
        for f in self.view:
            f.intercept()

        assert self.fetch("/flows/42/resume", method="POST").code == 200
        assert sum(f.intercepted for f in self.view) >= 1
        assert self.fetch("/flows/resume", method="POST").code == 200
        assert all(not f.intercepted for f in self.view)

    def test_kill(self):
        for f in self.view:
            f.backup()
            f.intercept()

        assert self.fetch("/flows/42/kill", method="POST").code == 200
        assert sum(f.killable for f in self.view) >= 1
        assert self.fetch("/flows/kill", method="POST").code == 200
        assert all(not f.killable for f in self.view)
        for f in self.view:
            f.revert()

    def test_flow_delete(self):
        f = self.view.get_by_id("42")
        assert f

        assert self.fetch("/flows/42", method="DELETE").code == 200

        assert not self.view.get_by_id("42")
        self.view.add([f])

        assert self.fetch("/flows/1234", method="DELETE").code == 404

    def test_flow_update(self):
        f = self.view.get_by_id("42")
        assert f.request.method == "GET"
        f.backup()

        upd = {
            "request": {
                "method": "PATCH",
                "port": 123,
                "headers": [("foo", "bar")],
                "trailers": [("foo", "bar")],
                "content": "req",
            },
            "response": {
                "msg": "Non-Authorisé",
                "code": 404,
                "headers": [("bar", "baz")],
                "trailers": [("foo", "bar")],
                "content": "resp",
            },
            "marked": ":red_circle:",
        }
        assert self.put_json("/flows/42", upd).code == 200
        assert f.request.method == "PATCH"
        assert f.request.port == 123
        assert f.request.headers["foo"] == "bar"
        assert f.request.text == "req"
        assert f.response.msg == "Non-Authorisé"
        assert f.response.status_code == 404
        assert f.response.headers["bar"] == "baz"
        assert f.response.text == "resp"

        upd = {
            "request": {
                "trailers": [("foo", "baz")],
            },
            "response": {
                "trailers": [("foo", "baz")],
            },
        }
        assert self.put_json("/flows/42", upd).code == 200
        assert f.request.trailers["foo"] == "baz"

        f.revert()

        assert self.put_json("/flows/42", {"foo": 42}).code == 400
        assert self.put_json("/flows/42", {"request": {"foo": 42}}).code == 400
        assert self.put_json("/flows/42", {"response": {"foo": 42}}).code == 400
        assert self.fetch("/flows/42", method="PUT", body="{}").code == 400
        assert (
            self.fetch(
                "/flows/42",
                method="PUT",
                headers={"Content-Type": "application/json"},
                body="!!",
            ).code
            == 400
        )

    def test_flow_duplicate(self):
        resp = self.fetch("/flows/42/duplicate", method="POST")
        assert resp.code == 200
        f = self.view.get_by_id(resp.body.decode())
        assert f
        assert f.id != "42"
        self.view.remove([f])

    def test_flow_revert(self):
        f = self.view.get_by_id("42")
        f.backup()
        f.request.method = "PATCH"
        self.fetch("/flows/42/revert", method="POST")
        assert not f._backup

    def test_flow_replay(self):
        with mock.patch("mitmproxy.command.CommandManager.call") as replay_call:
            assert self.fetch("/flows/42/replay", method="POST").code == 200
            assert replay_call.called

    def test_flow_content(self):
        f = self.view.get_by_id("42")
        f.backup()
        f.response.headers["Content-Disposition"] = 'inline; filename="filename.jpg"'

        r = self.fetch("/flows/42/response/content.data")
        assert r.body == b"message"
        assert r.headers["Content-Disposition"] == 'attachment; filename="filename.jpg"'

        del f.response.headers["Content-Disposition"]
        f.request.path = "/foo/bar.jpg"
        assert (
            self.fetch("/flows/42/response/content.data").headers["Content-Disposition"]
            == "attachment; filename=bar.jpg"
        )

        f.response.content = b""
        r = self.fetch("/flows/42/response/content.data")
        assert r.code == 200
        assert r.body == b""

        f.revert()

    def test_flow_content_returns_raw_content_when_decoding_fails(self):
        f = self.view.get_by_id("42")
        f.backup()

        f.response.headers["Content-Encoding"] = "gzip"
        # replace gzip magic number with garbage
        invalid_encoded_content = gzip.compress(b"Hello world!").replace(
            b"\x1f\x8b", b"\xff\xff"
        )
        f.response.raw_content = invalid_encoded_content

        r = self.fetch("/flows/42/response/content.data")
        assert r.body == invalid_encoded_content
        assert r.code == 200

        f.revert()

    def test_update_flow_content(self):
        assert (
            self.fetch("/flows/42/request/content.data", method="POST", body="new").code
            == 200
        )
        f = self.view.get_by_id("42")
        assert f.request.content == b"new"
        assert f.modified()
        f.revert()

    def test_update_flow_content_multipart(self):
        body = (
            b"--somefancyboundary\r\n"
            b'Content-Disposition: form-data; name="a"; filename="a.txt"\r\n'
            b"\r\n"
            b"such multipart. very wow.\r\n"
            b"--somefancyboundary--\r\n"
        )
        assert (
            self.fetch(
                "/flows/42/request/content.data",
                method="POST",
                headers={
                    "Content-Type": 'multipart/form-data; boundary="somefancyboundary"'
                },
                body=body,
            ).code
            == 200
        )
        f = self.view.get_by_id("42")
        assert f.request.content == b"such multipart. very wow."
        assert f.modified()
        f.revert()

    def test_flow_contentview(self):
        assert get_json(self.fetch("/flows/42/request/content/raw")) == {
            "lines": [[["text", "foo"]], [["text", "bar"]]],
            "description": "Raw",
        }
        assert get_json(self.fetch("/flows/42/request/content/raw?lines=1")) == {
            "lines": [[["text", "foo"]]],
            "description": "Raw",
        }
        assert self.fetch("/flows/42/messages/content/raw").code == 400

    def test_flow_contentview_websocket(self):
        assert get_json(self.fetch("/flows/43/messages/content/raw?lines=2")) == [
            {
                "description": "Raw",
                "from_client": True,
                "lines": [[["text", "hello binary"]]],
                "timestamp": 946681203,
            },
            {
                "description": "Raw",
                "from_client": True,
                "lines": [[["text", "hello text"]]],
                "timestamp": 946681204,
            },
        ]

    def test_commands(self):
        resp = self.fetch("/commands")
        assert resp.code == 200
        assert get_json(resp)["set"]["help"]

    def test_command_execute(self):
        resp = self.fetch("/commands/unknown", method="POST")
        assert resp.code == 200
        assert get_json(resp) == {"error": "Unknown command: unknown"}
        resp = self.fetch("/commands/commands.history.get", method="POST")
        assert resp.code == 200
        assert get_json(resp) == {"value": []}

    def test_events(self):
        resp = self.fetch("/events")
        assert resp.code == 200
        assert get_json(resp)[0]["level"] == "info"

    def test_options(self):
        j = get_json(self.fetch("/options"))
        assert type(j) == dict
        assert type(j["anticache"]) == dict

    def test_option_update(self):
        assert self.put_json("/options", {"anticache": True}).code == 200
        assert self.put_json("/options", {"wtf": True}).code == 400
        assert self.put_json("/options", {"anticache": "foo"}).code == 400

    def test_option_save(self):
        assert self.fetch("/options/save", method="POST").code == 200

    def test_conf(self):
        assert self.fetch("/conf.js").code == 200

    def test_err(self):
        with mock.patch("mitmproxy.tools.web.app.IndexHandler.get") as f:
            f.side_effect = RuntimeError
            assert self.fetch("/").code == 500

    @tornado.testing.gen_test
    def test_websocket(self):
        ws_url = f"ws://localhost:{self.get_http_port()}/updates"

        ws_client = yield websocket.websocket_connect(ws_url)
        self.master.options.anticomp = True

        r1 = yield ws_client.read_message()
        response = json.loads(r1)
        assert response == {
            "resource": "options",
            "cmd": "update",
            "data": {
                "anticomp": {
                    "value": True,
                    "choices": None,
                    "default": False,
                    "help": "Try to convince servers to send us un-compressed data.",
                    "type": "bool",
                }
            },
        }
        ws_client.close()

        # trigger on_close by opening a second connection.
        ws_client2 = yield websocket.websocket_connect(ws_url)
        ws_client2.close()
