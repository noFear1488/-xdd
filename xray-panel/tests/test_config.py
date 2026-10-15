import base64
import time
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from xcpanel import caddy, keys, xray
from xcpanel.model import CADDY_BEHIND_PORT, Client, Node, ValidationError, check_node, clean_transports

from .helpers import SETTINGS, local_node

NOW = 1_800_000_000


def client(cid=1, name="Alice", **kw):
    return Client(id=cid, name=name, uuid=f"00000000-0000-4000-8000-00000000000{cid}", sub_token=f"t{cid}", **kw)


class KeysTest(unittest.TestCase):
    def test_rfc7748_vector(self):
        k = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
        u = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
        self.assertEqual(
            keys.x25519(k, u).hex(), "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"
        )

    def test_keypair_matches_xray(self):
        # Pair produced by `xray x25519` (Xray 26.3).
        private = "iKzFYD0ipZGX-y7VQI8Fe_sPJ4wPPgPs3xI6hFpoQ2c"
        self.assertEqual(keys.public_from_private(private), "f_v1lGDOJu2atAom7kYvHZJJXA1lZr2toiJlnLsSFGo")

    def test_generated_pair_is_consistent(self):
        private, public = keys.reality_keypair()
        self.assertEqual(keys.public_from_private(private), public)
        self.assertEqual(len(keys.unb64(public)), 32)


class ClientTest(unittest.TestCase):
    def test_status(self):
        self.assertEqual(client().status(NOW), "active")
        self.assertEqual(client(enabled=False).status(NOW), "disabled")
        self.assertEqual(client(expire_at=NOW - 1).status(NOW), "expired")
        self.assertEqual(client(limit_bytes=100, up_bytes=60, down_bytes=40).status(NOW), "limited")
        self.assertEqual(client(limit_bytes=100, up_bytes=99).status(NOW), "active")

    def test_email_keeps_id_for_non_latin_names(self):
        self.assertEqual(client(7, "Пётр").email, "7")
        self.assertEqual(client(8, "Mom's phone").email, "8.mom-s-phone")


class XrayConfigTest(unittest.TestCase):
    def test_only_active_clients_are_written(self):
        clients = [client(1), client(2, "Bob", enabled=False), client(3, "Eve", expire_at=NOW - 5)]
        config = xray.build_config(local_node(), clients, SETTINGS, NOW)
        for inbound in config["inbounds"]:
            self.assertEqual([c["email"] for c in inbound["settings"]["clients"]], ["1.alice"])

    def test_private_networks_are_blocked(self):
        config = xray.build_config(local_node(), [client()], SETTINGS, NOW)
        first = config["routing"]["rules"][0]
        self.assertEqual(first["outboundTag"], "block")
        self.assertIn("127.0.0.0/8", first["ip"])
        self.assertEqual(config["routing"]["domainStrategy"], "IPIfNonMatch")

    def test_tls_transports_listen_locally(self):
        node = local_node(ws={"enabled": True}, grpc={"enabled": True})
        config = xray.build_config(node, [client()], SETTINGS, NOW)
        listen = {i["tag"]: i["listen"] for i in config["inbounds"]}
        self.assertEqual(listen, {"xhttp": "127.0.0.1", "ws": "127.0.0.1", "grpc": "127.0.0.1", "reality": "0.0.0.0"})

    def test_reality_front_falls_back_to_caddy(self):
        config = xray.build_config(local_node(), [client()], SETTINGS, NOW)
        reality = next(i for i in config["inbounds"] if i["tag"] == "reality")
        rs = reality["streamSettings"]["realitySettings"]
        self.assertEqual(reality["port"], 443)
        self.assertEqual(rs["target"], f"127.0.0.1:{CADDY_BEHIND_PORT}")
        self.assertEqual(rs["xver"], 1)
        self.assertEqual(rs["serverNames"], ["nordpine.site"])
        self.assertEqual(reality["settings"]["clients"][0]["flow"], "xtls-rprx-vision")

    def test_reality_on_own_port_borrows_target(self):
        node = local_node(reality={"port": 8443})
        rs = xray.build_config(node, [client()], SETTINGS, NOW)["inbounds"][1]["streamSettings"]["realitySettings"]
        self.assertEqual(rs["target"], "www.microsoft.com:443")
        self.assertEqual(rs["serverNames"], ["www.microsoft.com"])


class LinkTest(unittest.TestCase):
    def parse(self, url):
        parts = urlsplit(url)
        return parts, {k: v[0] for k, v in parse_qs(parts.query).items()}

    def test_xhttp_link(self):
        node = local_node()
        parts, q = self.parse(xray.link(client(), node, "xhttp", SETTINGS))
        self.assertEqual((parts.scheme, parts.hostname, parts.port), ("vless", "nordpine.site", 443))
        self.assertEqual(q["type"], "xhttp")
        self.assertEqual(q["path"], node.transports["xhttp"]["path"])
        self.assertEqual(q["security"], "tls")
        self.assertEqual(unquote(parts.fragment), "Main · XHTTP")

    def test_reality_link_matches_server(self):
        node = local_node()
        _, q = self.parse(xray.link(client(), node, "reality", SETTINGS))
        t = node.transports["reality"]
        self.assertEqual(q["pbk"], t["public_key"])
        self.assertEqual(q["sid"], t["short_id"])
        self.assertEqual(q["sni"], "nordpine.site")
        self.assertEqual(q["flow"], "xtls-rprx-vision")

    def test_ipv6_address_is_bracketed(self):
        node = Node(id=2, name="v6", address="2001:db8::1", caddy=False, transports=local_node(reality={"port": 443}).transports)
        node.transports["xhttp"]["enabled"] = False
        url = xray.link(client(), node, "reality", SETTINGS)
        self.assertIn("@[2001:db8::1]:443?", url)

    def test_subscription_is_base64_lines(self):
        links = [x["url"] for x in xray.client_links(client(), [local_node()], SETTINGS)]
        decoded = base64.b64decode(xray.subscription(links)).decode()
        self.assertEqual(decoded.splitlines(), links)
        self.assertEqual(len(links), 2)


class CaddyTest(unittest.TestCase):
    def test_front_mode(self):
        text = caddy.build_caddyfile(local_node(), SETTINGS)
        self.assertIn(f"https_port {CADDY_BEHIND_PORT}", text)
        self.assertIn("proxy_protocol", text)
        self.assertIn("\tbind 127.0.0.1", text)
        self.assertIn("http://nordpine.site {", text)
        self.assertIn("@panel path /panelpath123 /panelpath123/*", text)
        self.assertIn("reverse_proxy h2c://127.0.0.1:10001", text)

    def test_classic_mode(self):
        text = caddy.build_caddyfile(local_node(reality={"port": 8443}), SETTINGS)
        self.assertNotIn("https_port", text)
        self.assertNotIn("bind", text)

    def test_remote_node_has_no_panel(self):
        node = local_node()
        node.ssh_host, node.address = "root@203.0.113.7", "de.nordpine.site"
        text = caddy.build_caddyfile(node, SETTINGS)
        self.assertNotIn("@panel", text)
        self.assertIn("de.nordpine.site {", text)


class ValidationTest(unittest.TestCase):
    def test_port_collision(self):
        node = local_node(ws={"enabled": True, "port": 10001})
        with self.assertRaisesRegex(ValidationError, "already used"):
            check_node(node, SETTINGS)

    def test_path_shadowing_panel(self):
        node = local_node(xhttp={"path": "/panelpath123"})
        with self.assertRaisesRegex(ValidationError, "the panel"):
            check_node(node, SETTINGS)

    def test_tls_needs_domain(self):
        node = local_node()
        node.ssh_host, node.address = "root@203.0.113.7", "203.0.113.7"
        with self.assertRaisesRegex(ValidationError, "domain"):
            check_node(node, SETTINGS)

    def test_reality_only_ip_node_without_caddy_is_fine(self):
        node = Node(id=2, name="ip", address="203.0.113.7", ssh_host="root@203.0.113.7", caddy=False,
                    transports=local_node().transports)  # fmt: skip
        node.transports["xhttp"]["enabled"] = False
        check_node(node, SETTINGS)

    def test_clean_transports_keeps_private_key(self):
        current = local_node().transports
        cleaned = clean_transports({"reality": {"enabled": True, "port": 443}}, current)
        self.assertEqual(cleaned["reality"]["private_key"], current["reality"]["private_key"])
        rotated = clean_transports({"reality": {"enabled": True, "regenerate": True}}, current)
        self.assertNotEqual(rotated["reality"]["private_key"], current["reality"]["private_key"])

    def test_bad_path_rejected(self):
        with self.assertRaises(ValidationError):
            clean_transports({"xhttp": {"enabled": True, "path": "/a b{"}}, local_node().transports)


if __name__ == "__main__":
    unittest.main()
