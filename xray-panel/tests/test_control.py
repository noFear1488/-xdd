import json
import time
import unittest

from xcpanel.control import Controller
from xcpanel.deploy import deploy_script, parse_stats

from .helpers import FakeRunner, make_store

STATS = json.dumps(
    {
        "stat": [
            {"name": "user>>>1.alice>>>traffic>>>uplink", "value": "1000"},
            {"name": "user>>>1.alice>>>traffic>>>downlink", "value": 5000},
            {"name": "inbound>>>xhttp>>>traffic>>>downlink", "value": 99},
            {"name": "user>>>garbage>>>traffic>>>uplink", "value": 7},
        ]
    }
)


class StatsTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_stats(STATS), {1: (1000, 5000)})
        self.assertEqual(parse_stats("{}"), {})
        self.assertEqual(parse_stats("not json"), {})


class ScriptTest(unittest.TestCase):
    def test_order_follows_port_443(self):
        front = deploy_script("{}", "x", "<html>", caddy_first=True)
        self.assertLess(front.index("systemctl reload caddy"), front.index("systemctl restart xray"))
        classic = deploy_script("{}", "x", "<html>", caddy_first=False)
        self.assertLess(classic.index("systemctl restart xray"), classic.index("systemctl reload caddy"))

    def test_everything_validated_before_anything_applied(self):
        script = deploy_script("{}", "x", "<html>", caddy_first=True)
        self.assertLess(script.index("caddy validate"), script.index("install -m 644"))
        self.assertLess(script.index("xray run -test"), script.index("install -m 644"))

    def test_payload_cannot_break_out_of_heredoc(self):
        script = deploy_script("XCPANEL_EOF\n$(reboot)", None, "", caddy_first=False)
        self.assertNotIn("$(reboot)", script)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.addCleanup(self.store.close)
        self.runner = FakeRunner()
        self.ctl = Controller(self.store, self.runner)
        self.node_id = self.store.nodes()[0].id

    def test_first_deploy_then_nothing_to_do(self):
        self.ctl.reconcile()
        self.assertEqual(len(self.runner.deploys()), 1)
        self.ctl.reconcile()
        self.assertEqual(len(self.runner.deploys()), 1)

    def test_new_client_redeploys_xray_only(self):
        self.ctl.reconcile()
        self.store.add_client("Alice")
        self.ctl.reconcile()
        last = self.runner.deploys()[-1]
        self.assertIn("systemctl restart xray", last)
        self.assertNotIn("reload caddy", last)

    def test_traffic_limit_is_enforced(self):
        client = self.store.add_client("Alice", limit_bytes=4000)
        self.ctl.reconcile()
        self.runner.stats.append(STATS)
        self.ctl.collect_all()
        self.assertEqual(self.store.client(client.id).used, 6000)
        self.assertEqual(self.store.client(client.id).status(time.time()), "limited")
        self.assertTrue(self.ctl.pending(self.store.node(self.node_id)))
        self.ctl.reconcile()
        self.assertFalse(self.ctl.pending(self.store.node(self.node_id)))
        self.assertNotIn(client.uuid, self.ctl.render(self.store.node(self.node_id))[0])

    def test_failed_deploy_is_not_hammered(self):
        self.runner.fail_next = True
        self.ctl.reconcile()
        self.assertFalse(self.store.node(self.node_id).deploy_ok)
        self.ctl.reconcile()  # same config, failed a moment ago: wait
        self.assertEqual(len(self.runner.deploys()), 1)
        self.store.add_client("Alice")  # config changed: try right away
        self.ctl.reconcile()
        self.assertEqual(len(self.runner.deploys()), 2)
        self.assertTrue(self.store.node(self.node_id).deploy_ok)

    def test_stats_read_before_restart(self):
        self.store.add_client("Alice")
        self.ctl.reconcile()
        self.runner.stats.append(STATS)
        self.store.add_client("Bob")
        self.ctl.reconcile()
        kinds = ["stats" if "statsquery" in s else "deploy" for s in self.runner.scripts]
        self.assertEqual(kinds[-2:], ["stats", "deploy"])
        self.assertEqual(self.store.client(1).used, 6000)

    def test_disabled_node_is_emptied_not_frozen(self):
        alice = self.store.add_client("Alice")
        self.ctl.reconcile()
        self.store.update_node(self.node_id, enabled=False)
        self.ctl.reconcile()
        self.assertNotIn(alice.uuid, self.ctl.render(self.store.node(self.node_id))[0])
        self.assertFalse(self.ctl.pending(self.store.node(self.node_id)))  # and it was pushed


if __name__ == "__main__":
    unittest.main()
