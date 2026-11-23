import json
import unittest

from psclient import bridge_dc, custom_data, division, push, rest


class PushTest(unittest.TestCase):
    def test_change_user_status(self):
        msg = json.loads(push.change_user_status("v", "t1", "123", "tok"))
        self.assertEqual(msg["method"], 1003)
        self.assertEqual(msg["accountId"], 123)
        self.assertEqual(msg["auth"], {"accessToken": "tok"})

    def test_keep_alive_type(self):
        self.assertEqual(json.loads(push.change_keep_alive_status_type("v", "t", 3))["keepAliveStatusType"], 3)
        with self.assertRaises(ValueError):
            push.change_keep_alive_status_type("v", "t", 1)

    def test_url_and_packet(self):
        self.assertEqual(push.socket_url("h.example"), "wss://h.example/np/pushNotification")
        self.assertEqual(push.parse_packet('{"dataType":"x","contexts":["c"]}')["contexts"], ["c"])


class BridgeChannelTest(unittest.TestCase):
    def test_request_envelope(self):
        msg = json.loads(bridge_dc.set_all_muted_peer(1, True))
        self.assertEqual(list(msg), ["category", "type", "id", "payload"])
        self.assertEqual(msg, {"category": "request", "type": "setAllMutedPeer", "id": 1, "payload": {"muted": True}})

    def test_pub_sub_uses_same_list(self):
        payload = json.loads(bridge_dc.set_media_pub_sub_peers(2, ["a", "b"]))["payload"]
        self.assertEqual(payload["mediaSubscriptionPeers"], ["a", "b"])
        self.assertEqual(payload["mediaPublishmentPeers"], ["a", "b"])

    def test_floor_control_minimum(self):
        payload = json.loads(bridge_dc.set_floor_control_parameters(3, 4))["payload"]
        self.assertEqual(payload, {"algorithm": "sieVadAudioLevelBased01", "parameters": {"maxAudioStreams": 8}})

    def test_receiving_capability(self):
        payload = json.loads(bridge_dc.set_receiving_capability(4))["payload"]
        self.assertEqual(payload["receivingCapability"]["audio"]["opus"], [bridge_dc.CELT_24, bridge_dc.CELT_40])

    def test_request_ids_start_at_one(self):
        ids = bridge_dc.RequestIds()
        self.assertEqual([ids.next(), ids.next()], [1, 2])

    def test_parse_frames(self):
        resp = bridge_dc.parse_frame('{"category":"response","type":"setAllMutedPeer","id":5,"payload":{}}')
        self.assertEqual((resp.api, resp.request_id), ("setAllMutedPeer", 5))
        err = bridge_dc.parse_frame('{"category":"response","error":{"id":6,"code":"Bad","message":"m"}}')
        self.assertIsInstance(err, bridge_dc.ErrorResponse)
        self.assertEqual(err.request_id, 6)
        event = bridge_dc.parse_frame('{"type":"peerCreated","payload":{"peer":{"peerId":"p1"}}}')
        self.assertEqual((event.type, event.peer_id), ("peerCreated", "p1"))
        with self.assertRaises(ValueError):
            bridge_dc.parse_frame('{"type":"somethingElse"}')


class RestTest(unittest.TestCase):
    def test_create_peer(self):
        spec = rest.create_peer("B1", "tok", "TITLE")
        self.assertEqual(spec.method, "POST")
        self.assertEqual(spec.url(), "https://m.np.playstation.com/api/rtcBridge/v1/bridges/B1/peers?allow_duid_duplication=false&g=2")
        self.assertEqual(spec.headers["X-PSN-BRIDGE-TOKEN"], "tok")
        self.assertEqual(spec.headers["X-PSN-RTC-TITLE-ID"], "TITLE")
        opus = spec.body["mediaTypes"]["audio"]["opus"]
        self.assertEqual([o["bitrate"] for o in opus], [24, 40])
        self.assertEqual(opus[0]["samplingRates"], [16000])

    def test_answer_body(self):
        spec = rest.send_answer("B1", "P1", "tok", "T", "v=0")
        self.assertEqual(spec.path, "/v1/bridges/B1/peers/P1/answer")
        self.assertEqual(spec.body, {"answer": {"sdp": "v=0"}})

    def test_create_bridge_has_no_bridge_token(self):
        spec = rest.create_bridge("T")
        self.assertNotIn("X-PSN-BRIDGE-TOKEN", spec.headers)
        self.assertEqual(spec.body, {})

    def test_parse_peer_response(self):
        self.assertEqual(rest.parse_peer_response({"peer": {"peerId": "P"}}), ("P", None))
        offer = {"sdp": "v=0", "mids": []}
        self.assertEqual(rest.parse_peer_response({"peer": {"peerId": "P", "offer": offer}}), ("P", offer))

    def test_bridge_from_session(self):
        self.assertIsNone(rest.bridge_from_session({"bridges": []}))
        body = {"bridges": [{"bridgeId": "B", "bridgeToken": "t", "bridgeEtag": "e"}]}
        self.assertEqual(rest.bridge_from_session(body), ("B", "t"))

    def test_join_and_create_bodies(self):
        member = rest.member_object("ctx-1")
        self.assertEqual(member["accountId"], "me")
        self.assertEqual(member["platform"], "me")
        self.assertEqual(member["pushContexts"], [{"contextId": "ctx-1"}])
        self.assertEqual(len(rest.new_context_id()), 36)
        join = rest.join_party("S", member)
        self.assertEqual(join.path, "/v2/partySessions/S/members")
        self.assertEqual(join.body, {"members": [member]})
        create = rest.create_party("C", member)
        # The accepted request contains a one-element array.
        self.assertIsInstance(create.body["v2PartySessions"], list)
        self.assertEqual(len(create.body["v2PartySessions"]), 1)
        self.assertEqual(create.body["v2PartySessions"][0]["sessionType"], "ROOM")
        self.assertEqual(create.body["v2PartySessions"][0]["members"], [member])
        leave = rest.leave_party("S", "A")
        self.assertEqual((leave.method, leave.body), ("DELETE", None))

    def test_get_party_uses_collection_route(self):
        spec = rest.get_party("S")
        self.assertEqual(spec.path, "/v2/partySessions")
        self.assertEqual(spec.query, {"view": "v3"})
        self.assertEqual(spec.headers["X-PSN-SESSION-MANAGER-SESSION-IDS"], "S")

    def test_publish_bridge_peer(self):
        spec = rest.publish_bridge_peer("S", "me", "B", "P")
        self.assertEqual((spec.method, spec.path), ("PATCH", "/v2/partySessions/S/members/me"))
        self.assertEqual(custom_data.metadata_fields(spec.body["customData4"]),
                         ["", "", "1", "0", "6.2.0", "0", "0", "0", "B\nP", ""])


class CustomDataTest(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(custom_data.parse_bridge_info(custom_data.format_bridge_info("B", "P")), ("B", "P"))
        self.assertIsNone(custom_data.parse_bridge_info("no-newline"))


class DivisionTest(unittest.TestCase):
    def test_single_part(self):
        self.assertEqual(division.encode(2, b"{}", 100), [b"2/1/1\n{}"])

    def test_split_and_reassemble(self):
        payload = bytes(range(50))
        frames = division.encode(1, payload, 22 + 20)
        self.assertEqual(len(frames), 3)
        r = division.Reassembler()
        self.assertIsNone(r.feed("u", frames[0]))
        self.assertIsNone(r.feed("u", frames[1]))
        self.assertEqual(r.feed("u", frames[2]), (1, payload))

    def test_out_of_order_drops(self):
        frames = division.encode(1, b"x" * 30, 22 + 10)
        r = division.Reassembler()
        r.feed("u", frames[0])
        self.assertIsNone(r.feed("u", frames[2]))
        self.assertIsNone(r.feed("u", frames[1]))


if __name__ == "__main__":
    unittest.main()
